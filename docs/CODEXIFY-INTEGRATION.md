# Integrating Agent-DAW into Codexify

This is the design for what you described: **Music Projects as objects in
Codexify, with Threads and chats**, where the Studio Producer is one of
the roles a thread talks to.

Everything below is grounded in the actual Codexify codebase at
`/Volumes/Dev_SSD/Codexify-main`, not in a guess about how it works. File
references are `path:line`.

**Nothing here requires writing to Codexify from this repository.** The
adapter ships inside Agent-DAW; the Codexify-side wiring is a small PR
against Codexify's own review process.

---

## 1. The mapping, and why it is almost entirely reuse

Codexify already has the object model this needs.

| Music concept | Codexify object | Where |
|---|---|---|
| Music Project | `Project` | `guardian/db/models.py:945` |
| Thread | `ChatThread` | `guardian/db/models.py:1232` |
| Chat | `ChatMessage` | `guardian/db/models.py:1408` |
| Approved patches | `VectorStore` (Chroma) + a JSON index | `guardian/vector/store.py:129` |
| Stems, MIDI, project file | `MediaAsset` | `guardian/db/models.py:3877` |
| The Producer's turn | `AgentRun` / `AgentRunStep` | `guardian/db/models.py:5606` |

**There is no `chats` table.** "Chat" is the request-surface name; every
chat endpoint is thread-keyed (`POST /chat/threads`,
`POST /chat/{thread_id}/complete`, `GET /chat/{thread_id}/messages` --
`guardian/routes/chat.py:2289`, `:2822`, `:2731`). So "Music Projects have
Threads and chats" needs **no parallel concept at all** -- a Music Project
is a `Project`, and its threads and chats are the ordinary ones.

`ChatThread` already carries the two things a Music Project needs:

- `project_id` FK to `projects.id` (`:1243`) -- nullable, so a thread can
  exist before it is attached to a project.
- `active_profile_id` / `active_profile_revision` (`:1247`) -- a thread
  already has its own persona binding, which is how "this thread is
  talking to the Producer" is expressed.

### What actually needs adding

One discriminator. Either a `kind` / `domain` column on `projects`, or a
`music_projects` table with an FK to `projects.id`. The second is
cleaner: it leaves the generic `projects` table alone and keeps
music-specific fields (default library, default tempo, handoff target)
out of everyone's way.

**The obvious column is not available.** `projects` already has
`system_role`, but it cannot carry this. Its CHECK constraint allows
only `'general'` and `'imports'` (`guardian/db/models.py:961`), it means
"system-owned" rather than "has a domain", and it carries a partial
unique index -- `uq_projects_user_id_system_role`, unique over
`(user_id, system_role)` where `system_role IS NOT NULL` (`:993-1000`).
That index means a user could have exactly **one** music project under
`system_role`. It is a column for "the system made this", not for "this
is a music project".

There is precedent for the separate-table shape: `media_assets` is
already a project-scoped side table (`project_id` FK, `thread_id`
nullable) rather than a column on `projects`. A `music_projects` table
with `project_id` as its PK mirrors that exactly.

Everything else -- threads, messages, artifacts, retrieval, the command
bus -- is reused as-is.

---

## 2. Four ways in, ranked

### A. Route-mounted tools -- recommended

Codexify derives its agent-callable tools from the OpenAPI schema.
`guardian/command_bus/manifest.py:24` walks `app.openapi()` into
`CommandSpec`s; `guardian/tools/derive.py:103` turns those into
`ToolSpec`s. **Mounting a route automatically makes it a tool the model
can call.** There is no separate tool registry to update.

Agent-DAW ships that adapter:

```
agentdaw/codexify/router.py    -> build_router(prefix="/api/music")
agentdaw/service.py            -> the operations, as plain data in and out
```

The Codexify-side PR is thin:

```python
# guardian/routes/music_projects.py
from agentdaw.codexify.router import build_router

router = build_router(prefix="/api/music")
```

and in `guardian/guardian_api.py`, mounted the way Codexify mounts its
own routers (`_include_router`, `:252`):

```python
_include_router(
    label="music_projects",
    flag_name="CODEXIFY_ENABLE_MUSIC_ROUTES",
    include_fn=lambda: app.include_router(music_projects.router),
    core_surface=True,
)
```

**The gotcha that will bite you:** Codexify *quarantines* any router
whose label is not listed in the active supported profile
(`guardian/core/supported_profile.py:38-46`; labels live in
`config/supported_profiles/*.yaml` under `route_posture`). A label that
is missing from that YAML gets mounted but never advertised, and the
routes silently never appear in the tool manifest. Add `music_projects`
to the profile YAML in the same PR. The default profile is
`v1-local-core-web-mcp` (`guardian/core/supported_profile.py:15`).

Also note: routes marked `internal_only` are deliberately popped out of
`app.openapi()` by `_custom_openapi`, and therefore **never reach the
tool manifest**. The Producer's routes are meant to be callable, so they
must not be marked internal.

The surface the model sees:

| Route | Purpose |
|---|---|
| `POST /api/music/compose` | brief -> spec, stems, MIDI, mix, project, measurements |
| `POST /api/music/revise` | the Artist's correction -> a complete new spec |
| `POST /api/music/preview` | one patch, fast, under the drum bed |
| `POST /api/music/render` | spec -> artifacts |
| `GET  /api/music/anchors` | find approved patches for a request |
| `POST /api/music/approve` | promote a session's patches into the library |

The request bodies are named for the Artist's vocabulary on purpose.
`brief`, `feedback`, `query` -- not `params`, not `settings`. The schema
*is* the model's only documentation, so the field names have to read as
English to the Artist and the model at the same time.

### B. A service plugin -- the isolated alternative, now implemented

Codexify has a second, older extension seam: **service plugins**, which
are separate HTTP services Codexify calls. The TTS plugin
(`plugins/chatterbox/manifest.json`, `backend/tts_service/`) is the
working example.

The contract is the canonical service-plugin protocol v1.0:

- **Discovery**: `plugins/<id>/manifest.json`, validated by
  `guardian/plugins/plugin_manifest.py` (`PluginManifest`, `extra="forbid"`).
  It carries `schema_version: "1.0"`, `id`, `name`, `version`,
  `base_url` (http/https, host, no path/query/fragment), and
  `capabilities: [{id, actions: [...]}]`.
- **Invocation**: `POST {base_url}/invoke` with the envelope
  `{protocol_version, plugin_id, capability, action, input, context}`
  where `context` is `{request_id, thread_id, user_id}`.
  `guardian/core/plugins.py::invoke_plugin` posts it; the response must
  be `{"ok": bool, "output": {...} | null, "error": {...} | null}`.
  `_normalize_response` raises on anything else, so `ok` being a
  boolean is not negotiable.
- **Health**: `GET {base_url}/health`, 2-second timeout
  (`HEALTH_TIMEOUT_SECONDS`).
- **Awareness**: `guardian/graph/capability_index.py` merges the
  manifest's capabilities into the prompt layer, so the model is *told*
  the plugin exists and what it can do.

Agent-DAW implements this seam:

```
agentdaw/serve.py                              -> /health and /invoke
agentdaw/codexify/plugins/agentdaw/manifest.json
```

The manifest has been validated against Codexify's own `PluginManifest`
model (not against my reading of it), and a test asserts that the
manifest's action list and the service's `ACTIONS` cannot drift apart --
otherwise Codexify advertises a capability the service then refuses.

The `context.thread_id` is load-bearing: the service namespaces artifacts
under it, so the host can attribute files to the thread that made them.

**The hard constraint: Codexify's plugin facade has a 10-second invoke
timeout** (`INVOKE_TIMEOUT_SECONDS = 10`). A live compose takes about
**107 seconds**, because the Producer's LLM call is the long pole -- the
render itself is only ~2 seconds. A synchronous `compose` would be killed
mid-flight. So the service implements an async job path: `input.mode =
"async"` returns a job handle immediately and the host polls
`GET /jobs/{job_id}`. `render`, `preview`, `measure` and `anchor` are all
fast enough to stay synchronous.

### C. A skill -- the behavioural half

Codexify discovers skills on disk: project `.agents/skills` (first),
`.claude/skills`, `.cursor/skills`, `.codex/skills`, `.gemini/skills`;
user-global `~/.agents/skills`, `~/.claude/skills`, `~/.codex/skills`;
native `guardian/skills`; and plugin `guardian/plugins/*/skills`
(`guardian/skills/discovery.py:1-60`). The format is the open Agent
Skills one: `<skill-name>/SKILL.md` with YAML frontmatter (`name`,
`description`) and a markdown body.

Agent-DAW ships one:

```
agentdaw/codexify/skills/music-producer/SKILL.md
```

To install it user-globally:

```bash
cp -R agentdaw/codexify/skills/music-producer ~/.agents/skills/
```

The skill carries the rules that make the Producer behave: never write
DSP units, retrieve before inventing, report measurements rather than
adjectives, audition before committing, revise by delta, and keep the
Artist's stated tempo/key/length locked.

**A and C are complements, not alternatives.** The route is the
capability; the skill is the conduct. Without the skill the model has
tools but no reason to prefer a library anchor over a fresh invention.
The same is true of B: the plugin manifest is the capability, the skill
is still the conduct.

### D. An agent extension -- only if it should be proposable

Codexify has a first-class extension lifecycle: proposal -> install gate
-> registry -> binding -> activation -> dispatch (`guardian/extensions/`).
If Agent-DAW should be something the model can *propose installing*
rather than something always present, it needs a new
`ExtensionTargetSurface` member (`guardian/extensions/tokens.py:8-15`)
and an Alembic migration for the CHECK constraint on the registry table
(`guardian/db/models.py:848`).

This is the heaviest option and the least necessary right now. Skip it
until the always-on route proves out.

---

## 3. Where the approved patches live

The patch library is the part that makes the Producer compound, so it
wants to sit in Codexify's memory rather than in a file on one machine.

**Two layers, because retrieval needs both.**

*Semantic layer* -- the anchors as text in the vector store, in a
project-scoped namespace. `VectorStore.add_texts`
(`guardian/vector/store.py:129`) and `VectorStore.search` (`:178`) take
an explicit namespace; with none, resolution falls
`thread:{id}` -> `project:{id}` -> `global` (`:14-31`). For a Music
Project, `project:{id}` is right: an anchor approved in one thread of a
project should be findable from another thread of the same project.

*Measured layer* -- the deterministic index: the six macro values and the
measurements (LUFS, spectral centroid, band balance, stereo width). This
is what makes retrieval *measured* rather than verbal: "a brighter
version of that pad" is resolved against a centroid in Hz and a macro
position, not against a sentence embedding alone.

`agentdaw/library.py` already does the second half and is
storage-agnostic: it reads and writes plain JSON, so the same entries
can live in Codexify's memory, in a git-tracked file, or in a database
without the module caring. The Codexify-side adapter is responsible for
mirroring entries into the vector store and for keeping the JSON index
alongside.

Retrieval is exposed through `guardian/retrieve/api.py:13`
(`POST /api/retrieve`) and assembled into prompts by `ContextBroker`
(`guardian/context/broker.py:931`) under a policy in
`guardian/context/retrieval_router_policy.py`. A Music Project's anchors
would ride that path, scoped by `ScopeMode.project`.

---

## 4. Where the outputs live

`MediaAsset` (`guardian/db/models.py:3877`) is already a canonical media
registry with `media_kind` covering `audio`, a `project_id` FK that is
**NOT NULL**, a nullable `thread_id`, a `provenance` of
`uploaded|generated|imported|system`, `content_hash`, `storage_prefix`
and `mime_type`.

The service layer already emits artifacts in that shape, so the mapping
is one-to-one:

| Agent-DAW artifact | `media_kind` | `mime_type` | `provenance` |
|---|---|---|---|
| stem WAV | `audio` | `audio/wav` | `generated` |
| mix WAV | `audio` | `audio/wav` | `generated` |
| MIDI | `other` | `audio/midi` | `generated` |
| REAPER project | `other` | `text/plain` | `generated` |

Register each one against the Music Project, and against the thread that
produced it, so a thread's history shows what that thread actually made.

Note that Codexify has no single project-artifact registry table:
artifacts are split across `media_assets`, `generated_documents` /
`uploaded_documents`, and `agent_run_artifacts`
(`guardian/db/models.py:5756`). **`media_assets` is the right home for
audio**, because it is the only one with a `media_kind` that includes
`audio` and a project-scoped FK.

---

## 5. How a turn actually flows

```
Artist types in the Music Project's thread
        |
        v
ChatMessage (role=user)                   guardian/db/models.py:1408
        |
        v
POST /chat/{thread_id}/complete            guardian/routes/chat.py:2822
        |
        v
ChatCompletionTask -> Redis -> worker      guardian/tasks/types.py
        |                                  guardian/workers/chat_worker.py
        v
build_messages_for_llm                     guardian/core/chat_completion_service.py:5291
        |  - loads the thread (persona from active_profile_id)
        |  - resolves provider/model from thread_config
        |  - ContextBroker injects anchors from project:{id}
        v
model sees the music-producer skill + the /api/music/* tools
        |
        v
model calls POST /api/music/compose {"brief": "..."}
        |  (~107s: the LLM call is the long pole -- see section 6)
        v
Agent-DAW renders, measures, writes artifacts
        |
        v
model calls POST /api/music/approve -> anchors enter project:{id}
        |
        v
stems/MIDI/project registered as MediaAsset against the project
```

**A note on that diagram.** It draws compose as one blocking call, which
is the simple case and the one worth getting working first. But compose
takes ~107 seconds (section 6), and a chat turn that blocks that long
is a turn the Artist watches hang. The shape that survives contact with a
real chat is:

```
model calls compose -> returns a job handle          (fast)
        |
        v
the job runs; the thread reports back when it lands   (slow, out of band)
        |
        v
model calls preview / approve / render on the result  (fast)
```

Both integration paths need this. Path A needs the route to return a
handle; path B needs it because the plugin facade's ceiling is 10
seconds. The service layer already separates the operations that are
fast from the one that is not, so this is a matter of how the host
schedules the slow one.

The Producer does not need its own agent runtime inside Codexify. It is
a *tool* the model's thread can call, and the result is ordinary chat
output plus registered artifacts. If you later want the Producer to run
as a delegated sub-agent with its own run/step record, that machinery
already exists -- `AgentDeployment` / `AgentRun` / `AgentRunStep`
(`guardian/db/models.py:5559`, `:5606`, `:5661`), the
`/api/agents` router (`guardian/routes/agent_orchestration.py:50`), and
the delegation drafts at `/api/delegations`
(`guardian/routes/delegations.py:30`).

---

## 6. What the timings actually are

Measured on this machine against the live MiniMax gateway, not estimated:

| Operation | Time | What dominates |
|---|---|---|
| Compose (stub Producer, no LLM) | 3.8s end to end | 2.0s of DSP render; the rest is interpreter startup |
| Compose (live LLM) + render | **~107s** | the LLM call. The render is still only ~2s |
| Preview (one patch) | well under 1s | DSP only, no LLM |

The thinking budget was the obvious suspect, so it was profiled rather
than assumed. It is not the cause:

| Thinking budget | Compose latency |
|---|---|
| 4000 (default) | ~107s |
| 1024 | 137s |
| 256 | 159s |

No correlation -- if anything, lower budgets were slower. The latency
is inherent to the model on this gateway, and **cannot be tuned away**.

That number decides the integration, because Codexify's plugin facade
allows **10 seconds** per invoke. So:

- The **service-plugin path (B) must be asynchronous** for compose. The
  job API in `agentdaw/serve.py` exists for exactly this.
- The **route path (A) has no such ceiling**, but a chat turn that
  blocks for ~107 seconds on one tool call is a turn the Artist watches
  hang. The honest shape is the same as (B): start the job, return a
  handle, let the thread report back when it lands.

Either way, the fast operations are the ones that should be synchronous.
`preview`, `render`, `measure` and `anchor` are all sub-second to
two-second operations, and they are the ones an audition loop actually
uses.

## 7. The Producer's rules, and where they are enforced
These are not prompt conventions. Each is enforced in code, because a
model that can be talked into writing `cutoff: 2400` has broken the
system.

| Rule | Enforced in |
|---|---|
| The model never writes DSP units -- only six semantic axes | `agentdaw/spec.py` (Macros, SpectrumSpec, ShapeSpec, LfoSpec are all 0..1 semantic); the Producer's system prompt forbids numbers |
| Stated tempo, key, bars and length are locked | `extract_constraints()` / `check_constraints()` in `agentdaw/producer.py`, applied against the returned spec and retried on violation |
| Measurements, not adjectives | `agentdaw/analyze.py` + `report_findings()`; the service layer returns measurements, and the skill tells the model to quote them |
| The audition loop stays fast | `agentdaw/engine.py::render_preview`, under the session's drum bed, covered by a timing test |
| A revision is a complete new state | `agentdaw/library.py::PatchLibrary.revise` records a new entry with a complete macro vector and its parent; the parent is never mutated |
| An empty session is invalid | `SessionSpec.validate_references()` requires tracks, sections, and a patch for any MIDI track that has notes |

---

## 8. Wiring checklist

**In Agent-DAW (this repository, done):**

- [x] `agentdaw/service.py` -- the operations, JSON in and out
- [x] `agentdaw/codexify/router.py` -- the FastAPI adapter (path A)
- [x] `agentdaw/serve.py` -- the service-plugin adapter (path B)
- [x] `agentdaw/codexify/plugins/agentdaw/manifest.json` -- validated
      against Codexify's `PluginManifest`
- [x] `agentdaw/codexify/skills/music-producer/SKILL.md` -- the behaviour
- [x] `agentdaw/library.py` -- retrieval anchors and delta revisions
- [x] `--render-dir` and `--measurements` on `demo` / `compose`

**In Codexify, for path A (route-mounted):**

- [ ] `guardian/routes/music_projects.py` -- thin wrapper over `build_router`
- [ ] `_include_router(label="music_projects", ...)` in `guardian/guardian_api.py`
- [ ] Add `music_projects` to `config/supported_profiles/*.yaml` -- **or it is quarantined**
- [ ] Install the skill: `cp -R agentdaw/codexify/skills/music-producer ~/.agents/skills/`
- [ ] Get `agentdaw` importable in the backend image (see section 9)
- [ ] Alembic migration for the `music_projects` table
- [ ] Register artifacts as `MediaAsset` after each render
- [ ] Mirror approved anchors into `VectorStore` under `project:{id}`

**In Codexify, for path B (service plugin):**

- [ ] `plugins/agentdaw/manifest.json` -- copy from this repo
- [ ] A compose service for the image, plus a docker-compose service
      named `agentdaw` so `base_url` resolves
- [ ] Install the skill: `cp -R agentdaw/codexify/skills/music-producer ~/.agents/skills/`
- [ ] The same Alembic migration, `MediaAsset` registration and anchor
      mirroring as above -- a plugin does not change where state lives

---

## 9. Things to know before you start

- **Five of Agent-DAW's six dependencies are already in Codexify's
  locked backend requirements**: `numpy==2.2.6`, `scipy==1.16.2`,
  `pydantic==2.12.3`, `pyyaml==6.0.3`, `jsonschema==4.25.1`
  (`backend/requirements.txt`). Only `httpx` is missing -- and `httpx`
  is used solely by Agent-DAW's own LLM client. If Codexify supplies the
  inference (section 2's recommendation), `httpx` is not needed at all.
- **The backend image has no `git`.** `backend/Dockerfile` installs from
  a pre-compiled `backend/requirements.txt` and copies site-packages out
  of a builder stage; `git` is never installed. A git dependency
  therefore needs a Dockerfile change as well as network access at build
  time. Vendoring the package avoids both.
- **`from __future__ import annotations` breaks FastAPI request models
  defined inside a function.** The annotation becomes a string at
  runtime, and FastAPI resolves those strings against the defining
  module's globals -- so a model defined in a local scope cannot be
  resolved, and FastAPI silently demotes the argument to a *query
  parameter*. Every request then 422s, while the route still appears in
  the OpenAPI schema looking plausible. Agent-DAW's request models live
  at module scope for this reason, and a test asserts on the generated
  schema.
- **`main` is PR-first.** Changes to `main` in Codexify go through a
  pull request. A task authorisation is not authorisation to bypass the
  review policy.
- **Do not push to a branch another executor owns.** If a branch is
  claimed, commit locally, notify the owner with the commit id and your
  validation, and wait for integration or relinquishment.
- **Run the suite with `python3.11 -m pytest` from the repo root.** The
  `python3` on PATH is 3.14 and has none of the dependencies.
- **The root `conftest.py` auto-authenticates `TestClient`.** It injects
  a default `X-API-Key` from `GUARDIAN_API_KEY` into every request, so a
  test that asserts "missing authentication -> 401" silently exercises
  the authenticated path instead. Send an explicit empty header to
  express "no credential".
- **`/tmp` holds a stale full copy of the Codexify repo.** A scratch
  script placed in `/tmp` puts `/tmp` first on `sys.path`, so
  `import guardian` resolves to the stale copy. Run scratch scripts from
  the repo root and confirm `module.__file__`.
- **REAPER's evaluation screen blocks startup until it is licensed.** A
  REAPER-driven render cannot be unattended on an unlicensed install.
  This does not affect the Codexify integration: Agent-DAW renders the
  stems itself, and REAPER is where the Artist takes over.
