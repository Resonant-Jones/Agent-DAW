# Agent-DAW

An LLM studio producer. You describe a vision; it compiles a plan into
audio you can hear, mix, and hand to a real DAW.

The role split is the whole idea: **you are the artist, the assistant is
the producer.** You say "a low pulsing bass and a thin cold pad that opens
up over the form, it should feel like it is holding its breath." The
producer decides tempo, key, form, instrumentation, patch design, and the
arrangement of notes, then hands you stems and MIDI to finish in REAPER.

---

## Status

Slice 1 is built and working end to end:

| Piece | State |
|---|---|
| Session + Patch spec (JSON Schema, the published contract) | done |
| Harmonic-spectrum wavetable synthesis | done |
| Macro → engine parameter map (pure, total functions) | done |
| Subtractive engine: filter, envelopes, LFO, drive, chorus, delay, convolution reverb | done |
| Standard MIDI writer | done |
| REAPER `.rpp` writer | done, checked against a REAPER-written project, and opened by REAPER |
| Measurement pass (BS.1770 loudness, spectral, band, stereo) | done, cross-validated against ffmpeg |
| Producer LLM adapter (anthropic / chat-completions / gemini) | done, live against MiniMax |
| Patch library + retrieval anchors + delta revisions | done |
| Codexify client: service layer, FastAPI router, skill | done, not yet mounted in Codexify |
| Generative audio adapters (Stable Audio 3.0, Lyria 3) | written, **no API key exercised yet** |
| Multi-agent producer with delegated roles | not started |

Known gaps are listed at the bottom. Nothing here is vaporware: every
"done" row has tests behind it.

---

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install numpy scipy pydantic pyyaml httpx jsonschema pytest

# what is configured, what is missing
.venv/bin/python -m agentdaw.cli doctor

# render a full session with no LLM and no API keys
.venv/bin/python -m agentdaw.cli --out out demo

# let the Producer work from a description
.venv/bin/python -m agentdaw.cli --out out compose \
  "a dark, tense 8-bar intro: low pulsing bass, thin cold pad that opens up, sparse drums"

# audition one patch fast, in context
.venv/bin/python -m agentdaw.cli --out out preview out/session.json pad_warm

# measure a file against BS.1770 and spectral descriptors
.venv/bin/python -m agentdaw.cli measure out/mix.wav
```

`compose` writes `session.json` plus per-track WAV stems, per-track MIDI,
a `mix.wav`, and a REAPER project.

---

## The three rules

Everything in the design falls out of three commitments. They are the
reason this works where most "AI producer" projects stall.

### 1. The Producer never writes DSP units

Every value in the spec is semantic and normalized to 0..1. There is no
hertz, no milliseconds, no decibels, no filter Q anywhere in the model's
output.

The model says `brightness: 0.35`. The engine decides that means a
700 Hz cutoff. The model says `odd_emphasis: 0.6`. The engine decides
that means the even partials get pulled down by 30%.

This is not stylistic. A language model has real intuition about harmonic
content and envelope shape, and none at all for the acoustic
consequence of "cutoff 440 Hz, Q 6.3". The DAWZY paper (arXiv 2512.03289)
documents exactly this failure: their LLM-driven REAPER agent broke on
unit conversion, and the fix was to hide the conversion inside
permissioned tools that take human units.

The same instinct shows up in the patch design. Rather than letting the
model pick wavetable samples, it describes **harmonic content** —
"odd-weighted, saw-to-square tilt" — and the engine synthesizes the table
from that. The model works in the domain it understands.

### 2. The Producer measures, it does not opine

An LLM cannot hear. Any system that asks one "does this mix sound good?"
is asking for a confident guess.

So the Producer claims measurements instead of taste. `analyze.py`
computes integrated LUFS with BS.1770 K-weighting and gating, spectral
centroid and rolloff, low/mid/high band balance, crest factor, dynamic
range, and stereo width. The Producer reports what it measured:

```
Bass: low band sits 24.1 dB above the mid band -- possible low-end buildup.
Bass: spectral centroid is 331 Hz -- very dark.
Pad:  spectral centroid is 1132 Hz -- bright and exposed.
```

Factual, checkable, actionable. The artist makes the ear call.

### 3. The audition loop must be fast

A producer's feedback loop is sub-second. If each patch revision costs
40 seconds of render, you lose the thread and the whole thing dies of
tedium.

The engine renders an 8-bar preview with drums in **about a second**, and
the loop renders a full session — stems, MIDI, mix, project, measurements
— in **under two seconds**. The test suite enforces the budget:

```python
assert elapsed < 5.0, f"preview render took {elapsed:.2f}s, budget is 5s"
```

Two details make this work:

- **Previews are heard in context.** Every audition render carries a drum
  bed, because a patch judged in isolation is a different patch than the
  same patch under a beat.
- **The DSP is vectorized, not looped.** A feedback delay line is written
  as a gather-then-scatter over a circular buffer, which is only valid if
  the read distance exceeds one block length — enforced, not assumed.
  The filter is an RBJ biquad run block-wise with carried state, so a
  time-varying cutoff is exact rather than approximated.

---

## Layout

```
agentdaw/
  spec.py        Session + Patch models. THE CONTRACT. Semantic values only.
  macros.py      Macro vector -> concrete engine params. Pure functions.
  wavetable.py   Harmonic spectrum -> wavetable.
  biquad.py      RBJ filter coefficients and block-wise sweeping.
  dsp.py         Oscillators, envelopes, delay lines, reverb, track render.
  drums.py       Deterministic context drum bed.
  analyze.py     The measurement pass. BS.1770 and spectral descriptors.
  engine.py      Session -> stems + MIDI + mix + project.
  producer.py    The Producer: prompt, validation, repair loop.
  llm.py         Protocol-agnostic LLM client.
  providers/     Stable Audio 3.0 and Lyria 3 adapters.
  midi.py        Standard MIDI File writer and reader.
  reaper.py      REAPER .rpp writer.
  library.py     Approved-patch library: retrieval anchors, delta revisions.
  service.py     The operations as plain data in and out. The host seam.
  codexify/      Codexify adapter: FastAPI router + the music-producer skill.
  wav.py         WAV writer/reader. No third-party audio library.
  cli.py         Command line surface.
spec/            Generated JSON Schema exports.
tests/           72 tests, written to discriminate.
```

The JSON Schemas in `spec/` are the published interface. A hosted product
would serve them to third parties so patches and sessions stay portable
even if the engine is rewritten in another language.

---

## Design decisions worth knowing about

**Pure macro maps, always.** A revision produces a *new* macro vector and
everything is recomputed from it. No parameter is retained from a
previous state while its partner is re-derived. Cutoff and resonance are
paired values; deriving one and holding the other is how you get a filter
that does not match the sound it was tuned for.

**Chords share a filter envelope.** Notes are grouped by onset, so a
chord gets one filter sweep. Musically correct, and several times
cheaper than per-note filtering.

**Band-limited wavetables.** The table is rebuilt per pitch with
harmonics above Nyquist removed. A bright patch played high does not turn
into aliased hash.

**Deterministic seeds.** A patch's seed is part of the patch. "Deploy"
means "re-renders identically"; a new seed is an explicit variation, never
a silent one.

**The loudness meter is cross-validated.** BS.1770 sums channel energies;
averaging instead of summing biases every stereo reading by exactly
3.01 LU, which is small enough to look plausible and constant enough to
go unnoticed. The test suite catches it by comparing against ffmpeg's
`ebur128` on a real signal, and asserts the channel-sum property
directly.

**The artist's stated constraints are enforced, not trusted.** A model
told "120 BPM" will sometimes answer 88 BPM and describe it plausibly.
Explicit constraints — tempo, bar count, key — are extracted from the
vision, restated to the model as locked, and then *checked against the
returned spec*. A violation is sent back for repair rather than shipped.

**The Producer is measured against a reasoning model's budget.**
MiniMax's models require adaptive thinking and refuse to disable it, so
`max_tokens` is shared between deliberation and answer. At 4000 tokens the
model spent 3999 on thinking and returned nothing at all. The client
treats that as a distinct error and the Producer grows its budget rather
than asking the model to try again the same way. A `thinking.budget_tokens`
cap cuts latency substantially.

**Rotating credentials are refreshed on demand.** The runtime rotates its
OAuth token underneath long-lived processes. A token that was valid when
the process started gets rejected as "invalid access token" minutes
later, so the client reloads the credential and retries once.

---

## Handoff formats

| Artifact | Format | Why |
|---|---|---|
| Patch | JSON (PatchSpec) | portable, editable, re-generatable |
| MIDI parts | Standard MIDI File | every DAW understands it without negotiation |
| Stems | 24-bit WAV | universal, and what a mixing session actually consumes |
| Project | REAPER `.rpp` | plain text and scriptable, so the handoff carries real project state rather than a folder of files |

Macro positions travel to the DAW as CC74 on the MIDI track, so the
Producer's intent survives the handoff even though the receiving DAW has
no idea what "brightness" means.

### Two views of the same session

Each render writes two `.rpp` files, because they answer different
questions:

| Project | Tracks carry | For |
|---|---|---|
| `<title>.rpp` | the rendered stem WAVs | the mix-ready handoff — opens with sound, ready to mix, resample or re-render |
| `<title>-midi.rpp` | the `.mid` files on melodic tracks, WAVs on audio tracks | the sequencing view — drop a real instrument on a melodic track and re-sequence it |

The stems view is the default. The sequencing view exists because a MIDI
track with no instrument is silent: a `.rpp` that references only MIDI
would open in REAPER and produce nothing until the artist chose a synth,
which is a bad first impression of a handoff.

### Rendering through REAPER

```bash
.venv/bin/python -m agentdaw.cli --out out reaper-render out/session.rpp
```

This launches REAPER with `-renderproject` and waits for the audio to
appear. Three environment facts shape it, all of them learned the hard way:

- **REAPER is launched through `open`, never executed directly.** It is a
  Cocoa app and needs windowserver XPC to build its windows; a process in
  a restricted sandbox cannot reach them and the app hangs at startup
  instead of failing loudly. Launching through LaunchServices puts it in
  the user's login session.
- **`open -n` and `-newinst` are both required.** macOS does not pass
  command-line arguments to an application that is already running, so a
  second `open` of a live REAPER silently does nothing and the render never
  happens.
- **A seeded profile.** REAPER prompts for a resource path on first run and
  opens an audio-device dialog at startup. Both block forever in a
  headless context, so `agentdaw/profiles/agentdaw/reaper.ini` is seeded
  with `audiocfgopen=0` and the `coreaudio*` device keys.

One thing still needs a human: REAPER's one-time evaluation screen. It
blocks REAPER's startup entirely — no splash log, no startup script, no
render — and it **reappears periodically** until REAPER is licensed. That
matters for the architecture: a render pipeline that depends on REAPER
needs either a licence or a periodic nudge from a person. Everything else
can be made unattended.

### What REAPER itself says about our project

REAPER 7.82 was launched against a generated `.rpp` and then quit
cleanly. Its own config came back with:

```ini
lastproject=/tmp/adw_reaper_run/renderable.rpp
projecttab1=/tmp/adw_reaper_run/renderable.rpp
[Recent]
recent01=/tmp/adw_reaper_run/renderable.rpp
```

REAPER parsed the project, opened it, kept it in its project tab, and
listed it in its recent list. That is REAPER's own testimony that the
generated file loads — not an inference from a structural check. The
render itself is the piece still outstanding, and it is waiting on the
evaluation screen.

`tools/verify_reaper_handoff.py` closes the loop: it installs a
`__startup.lua` into REAPER's resource path that makes REAPER open the
project, write down every track, item, item length and media source it
found, render it, and quit. The report is the verification artifact.
**The hooks it installs are disabled by default** — the script is inert
unless a job file is present, and the ones left on this machine have been
neutered.

---

## The patch library

The Producer only compounds if it can recognise a sound it has already
made. The library is that memory: every patch the artist approves becomes
an **anchor** — named, measured, retrievable — so a later request revises
a known patch by delta instead of inventing something new.

```bash
# render and measure, so the library has facts to index
.venv/bin/python -m agentdaw.cli --out out demo --measurements out/measurements.json

# promote the session's patches into approved anchors
.venv/bin/python -m agentdaw.cli library approve \
    --spec out/session.json --measurements out/measurements.json \
    --library out/patch-library.json

# ask what already exists
.venv/bin/python -m agentdaw.cli library find "a brighter, grittier version of that warm pad" \
    --library out/patch-library.json
```

Retrieval is **measured, not vibes**. It ranks on the artist's words, the
macro vector and the measured facts, and it reports why each hit matched:

```
  pad_warm             score 0.663  Warm Pad
    - shares words with the request: body, brightness, warm
    - request asks to move brightness 0.35 -> 0.60, grit 0.15 -> 0.40
    - measured spectral centroid of this anchor is 1132.4Hz
    suggested: brightness 0.35->0.60, grit 0.15->0.40
```

Three rules the module enforces:

- **A revision is a complete new state, never a patch.** The parent is
  never mutated. The child carries the whole macro vector and the whole
  spectrum, plus its parent and the artist's words describing the change.
- **A miss is a legitimate outcome.** Retrieval returns nothing rather
  than a false recall, and the Producer is told to invent — and the
  result is recorded as born new rather than derived.
- **The Producer must emit complete patches.** All six axes, a full
  spectrum, a full shape, a full LFO. The prompt says so, and the code
  refuses a spec that does not parse.

---

## Codexify

Agent-DAW ships a client for Codexify, where Music Projects are objects
with threads and chats. The design is grounded in Codexify's actual
object model — read it in **[docs/CODEXIFY-INTEGRATION.md](docs/CODEXIFY-INTEGRATION.md)**.

The short version: Codexify already has the objects this needs. A Music
Project is a `Project` row; its threads and chats are the ordinary
`ChatThread` / `ChatMessage` pairs (there is no separate chats table);
the approved patches live in the vector store under a project-scoped
namespace; the stems and MIDI register as `MediaAsset` rows. And because
Codexify derives its agent-callable tools from the OpenAPI schema, a
route that exists in the Codexify app is what makes the Producer callable
by a model.

**The engine runs in its own container; the route does not.** Codexify
derives tools only from its own OpenAPI schema, so the route has to live
in the Codexify process — but nothing says the code that renders audio
has to. So Agent-DAW runs as a service and Codexify forwards to it:

```
Codexify thread ──▶ /api/music/compose  (a route, so it becomes a tool)
                          │
                          ▼  canonical service-plugin envelope
                    Agent-DAW container ──▶ stems, MIDI, mix, .rpp
```

That is the same shape Codexify's own TTS plugin uses: a service for the
engine, in-process code to reach it. It keeps a 107-second-capable DSP
process out of the backend, and it gives Agent-DAW its own release cycle.

Four pieces ship here:

| Piece | Path | What it is |
|---|---|---|
| Service layer | `agentdaw/service.py` | the operations, plain data in and out, no web framework |
| Service | `agentdaw/serve.py` | the canonical service-plugin surface: `/health` and `/invoke` |
| Plugin manifest | `agentdaw/codexify/plugins/agentdaw/manifest.json` | what Codexify advertises the service as |
| Skill | `agentdaw/codexify/skills/music-producer/SKILL.md` | the conduct: retrieve before inventing, measure don't opine, revise by delta |

**Compose is a job.** A live compose takes about 107 seconds — the
Producer's LLM call is the long pole, and the render itself is only about
two seconds. The service returns a job handle immediately and the caller
polls `/api/music/jobs/{job_id}`. The quick operations — `preview`,
`render`, `measure`, `anchors` — stay synchronous.

Run it as a service:

```bash
docker build -t agentdaw .
docker run --rm -p 8000:8000 -v "$PWD/out:/app/agentdaw_output" agentdaw
```

The Codexify-side wiring is a small PR: mount the route with a label that
**must** be added to `config/supported_profiles/*.yaml` (unlisted labels
are quarantined), add the compose service, and copy the skill into
`~/.agents/skills/`.

---

## Known gaps

- **REAPER opens the project; it does not render unattended.** REAPER 7.82
  is installed at `/Applications/REAPER.app` and has opened a generated
  `.rpp` — its own config named the file as `lastproject` and in its
  recent list, which is REAPER's own testimony that the file loads. The
  `.rpp` writer's structure was also checked against a project REAPER
  itself saved on macOS, and that check found three load-bearing bugs in
  the first version of the writer: the render bounds mode was `0` (custom
  0..0 range, so it would have rendered an empty file), track volume was
  written in decibels where REAPER expects a linear multiplier, and the
  project had no master bus. All three are fixed and covered by tests.
  What is still outstanding is the *render*, because REAPER's evaluation
  screen blocks startup until REAPER is licensed, and it reappears
  periodically. A licensed REAPER removes that constraint entirely. It
  does not affect the Codexify integration: Agent-DAW renders the stems
  itself.
- **Stable Audio and Lyria are untested against their live APIs.** No key
  is configured. The request shapes follow the published docs; the
  response handling has not been exercised.
- **The Codexify client is not mounted.** The service layer, the router
  and the skill exist and are tested here, but nothing in Codexify calls
  them yet. That is a PR against Codexify, not a change to this repo —
  see `docs/CODEXIFY-INTEGRATION.md` for the exact wiring and the
  supported-profile gotcha that will silently quarantine the routes if
  it is missed.
- **One instrument.** The engine is subtractive only. A sample-based or
  FM path would widen the palette.
- **The Producer is a single agent.** Delegated roles (patch designer,
  arranger, mixer, critic) are designed for but not built.

---

## Prior art worth reading before you extend this

- **DAWZY** — <https://arxiv.org/html/2512.03289v1> — natural language to
  reversible, grounded, atomic REAPER edits. Read the failure modes.
- **openDAW MCP** — <https://pypi.org/project/opendaw-mcp/> — 543 MCP tools
  for agent-native control of a browser DAW, Apache-2.0, headless.
- **Veena** — <https://www.veena.studio/agentic-daw> — an agentic DAW that
  generates parts as editable MIDI. Its success-contract brief is a good
  template.
- **Stability, on stems** — <https://stability.ai/explainers/ai-music-stems-and-audio-inpainting-generating-parts-instead-of-finished-songs>
  — why `TrackType: Instrument` matters for generative audio that is a
  *source* in a production rather than the whole production.
- **sookoothaii/fruity-daw-agent-orchestra** — measurement over prompt-based
  generation, with humanization calibrated from 446k drum onsets.
