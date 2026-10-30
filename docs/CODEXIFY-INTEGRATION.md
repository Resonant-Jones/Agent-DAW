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

Everything else -- threads, messages, artifacts, retrieval, the command
bus -- is reused as-is.

---

## 2. Three ways in, ranked

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
    router=music_projects.router,
    default_enabled=True,
)
```

**The gotcha that will bite you:** Codexify *quarantines* any router
whose label is not listed in the active supported profile
(`guardian/core/supported_profile.py:38-46`; labels live in
`config/supported_profiles/*.yaml` under `route_posture`). A label that
is missing from that YAML gets mounted but never advertised, and the
routes silently never appear in the tool manifest. Add `music_projects`
to the profile YAML in the same PR.

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

### B. A skill -- the behavioural half

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

**A and B are complements, not alternatives.** The route is the
capability; the skill is the conduct. Without the skill the model has
tools but no reason to prefer a library anchor over a fresh invention.

### C. An agent extension -- only if it should be proposable

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
        |
        v
Agent-DAW renders, measures, writes artifacts
        |
        v
model calls POST /api/music/approve -> anchors enter project:{id}
        |
        v
stems/MIDI/project registered as MediaAsset against the project
```

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

## 6. The Producer's rules, and where they are enforced

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

## 7. Wiring checklist

**In Agent-DAW (this repository, done):**

- [x] `agentdaw/service.py` -- the operations, JSON in and out
- [x] `agentdaw/codexify/router.py` -- the FastAPI adapter
- [x] `agentdaw/codexify/skills/music-producer/SKILL.md` -- the behaviour
- [x] `agentdaw/library.py` -- retrieval anchors and delta revisions
- [x] `--render-dir` and `--measurements` on `demo` / `compose`

**In Codexify (a PR against Codexify's own review process):**

- [ ] `guardian/routes/music_projects.py` -- thin wrapper over `build_router`
- [ ] `_include_router(label="music_projects", ...)` in `guardian/guardian_api.py`
- [ ] Add `music_projects` to `config/supported_profiles/*.yaml` -- **or it is quarantined**
- [ ] Install the skill: `cp -R agentdaw/codexify/skills/music-producer ~/.agents/skills/`
- [ ] Add `agentdaw` to the backend's requirements
- [ ] Alembic migration for the music-project discriminator
- [ ] Register artifacts as `MediaAsset` after each render
- [ ] Mirror approved anchors into `VectorStore` under `project:{id}`

---

## 8. Things to know before you start

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
