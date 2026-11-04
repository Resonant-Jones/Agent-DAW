# The Codexify-side patch

This directory holds the Codexify half of the integration, staged here so
it can be reviewed before it touches Codexify's repository.

It is **not** part of the `agentdaw` package. Nothing in `agentdaw/`
imports anything here.

## The shape

**The engine runs in its own service; the route does not.**

Codexify derives its model-callable tools from the app's OpenAPI schema
(`guardian/command_bus/manifest.py` -> `guardian/tools/derive.py`), so a
route has to exist in the Codexify process for the model to be able to
call it. But nothing says the code that renders audio has to live there.

So `/api/music/*` forwards over the canonical service-plugin protocol to
an Agent-DAW container, and Codexify never imports `agentdaw`. This is
the shape Codexify's own TTS plugin already uses: a service for the
engine, in-process code to reach it.

A compose takes about **107 seconds** -- the Producer's model call is the
long pole, and the render itself is about two seconds -- so `compose`
returns a job handle and the caller polls `/api/music/jobs/{job_id}`.

## What is here

| Path | What it is |
|---|---|
| `music-projects.patch` | A unified diff against four existing Codexify files |
| `guardian/routes/music_projects.py` | New: the route, the forwarding, and the artifact registration |
| `guardian/db/migrations/versions/a7d4c9e2f1b3_add_music_projects_table.py` | New: the Alembic migration |
| `config/supported_profiles/v1-music-producer.yaml` | New: a supported profile that enables the route |
| `plugins/agentdaw/manifest.json` | New: what Codexify advertises the service as |
| `guardian/skills/music-producer/SKILL.md` | New: the conduct |
| `tests/routes/test_music_projects.py` | New: the tests |

The compose service and the `guardian_api.py` mount are in the patch.

## How to apply it

From the root of a Codexify checkout:

```bash
git apply --check codexify/music-projects.patch   # dry run
git apply codexify/music-projects.patch
cp codexify/guardian/routes/music_projects.py guardian/routes/
cp codexify/guardian/db/migrations/versions/a7d4c9e2f1b3_add_music_projects_table.py \
   guardian/db/migrations/versions/
cp codexify/config/supported_profiles/v1-music-producer.yaml config/supported_profiles/
mkdir -p plugins/agentdaw
cp codexify/plugins/agentdaw/manifest.json plugins/agentdaw/
mkdir -p guardian/skills/music-producer
cp codexify/guardian/skills/music-producer/SKILL.md guardian/skills/music-producer/
cp codexify/tests/routes/test_music_projects.py tests/routes/
```

Then start the engine:

```bash
docker compose --profile music up -d agentdaw
```

and select the profile so the routes are not quarantined:

```bash
CODEXIFY_SUPPORTED_PROFILE=v1-music-producer docker compose up -d
```

## The supported profile is a separate file on purpose

`config/supported_profiles/v1-local-core-web-mcp.yaml` -- the default
profile -- is **byte-frozen**. There is a test that asserts it is
identical to HEAD:

```python
def test_default_profile_file_is_unchanged_from_head() -> None:
    expected = subprocess.check_output(
        ["git", "show", f"HEAD:config/supported_profiles/{DEFAULT_PROFILE_NAME}.yaml"],
        cwd=REPO_ROOT,
    )
    assert DEFAULT_PROFILE_PATH.read_bytes() == expected
```

So a new capability gets its own profile rather than a change to the
default one. `v1-music-producer.yaml` is a copy of the default with
`music_projects` enabled.

Every *other* profile leaves `music_projects` quarantined -- the same
invariant the Hosted Room route families hold -- so the Producer is
absent rather than broken wherever it was not asked for. There is a test
for that too.

## Where the outputs go

`MediaAsset` is already Codexify's canonical media registry, and it is
the only one of the three registries with a `media_kind` that includes
`audio` and a project-scoped foreign key
(`guardian/db/models.py:3877`). So the mapping is one-to-one:

| Agent-DAW artifact | `media_kind` | `mime_type` | `provenance` |
|---|---|---|---|
| stem WAV | `audio` | `audio/wav` | `generated` |
| mix WAV | `audio` | `audio/wav` | `generated` |
| MIDI | `other` | `audio/midi` | `generated` |
| REAPER project | `other` | `text/plain` | `generated` |

**The identity fields are computed by Codexify, not by the Producer.**
`deterministic_id`, `normalized_slug`, `system_name` and
`storage_prefix` are the host's rules, and `guardian/services/
media_identity.py::compute_identity` already implements them.
`register_artifacts()` calls that function rather than reimplementing
it, so the rules cannot drift. Agent-DAW supplies only the facts it
owns: the path, the media kind, the mime type, the byte count, and the
content hash.

The content hash is computed by the Producer while the bytes are already
in hand, so the host does not read every stem back off disk just to
hash it. It is the hash of the bytes *actually written to disk*, not of
what was rendered in memory -- otherwise the registry would index
something the Artist never heard.

Verified end to end against the real checkout: a render produces eight
artifacts, each one becomes a `MediaAsset` row whose NOT NULL columns
and CHECK constraints are satisfied, and audio lands under the `audio/`
storage prefix:

```
deterministic_id : 20261030-7d126d8f
normalized_slug  : pad
system_name      : 20261030-7d126d8f--pad.wav
storage_prefix   : audio/
```

## The one design decision worth arguing about

A Music Project is **an ordinary `Project` with a side row**, not a new
kind of `Project`.

The obvious alternative -- a `kind` column on `projects` -- was rejected
because `projects` already has `system_role`, which cannot do the job:
it means "system-owned", its CHECK allows only `'general'` and
`'imports'`, and the partial unique index `uq_projects_user_id_system_role`
allows one row per `(user_id, system_role)`. A user could therefore have
exactly **one** music project under it.

The separate-table shape has precedent: `media_assets` is already a
project-scoped side table with a `project_id` foreign key and a nullable
`thread_id`. `music_projects` mirrors that exactly, 1:1 with `projects`.

`current_spec` holds the complete session state and is the source of
truth. The scalar columns beside it (`bpm`, `musical_key`, `bars`, ...)
are a queryable projection of the same values -- "every project at 120
BPM" should not mean parsing JSON. They are deliberately not indexed:
the table is small and is almost always reached by `project_id`.

## What has been verified, and what has not

**Verified, against the real Codexify checkout:**

- The migration leaves **exactly one** Alembic head, and renders valid
  Postgres DDL in Alembic's offline mode, including
  `PRIMARY KEY (project_id)` and
  `FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE`.
- The `MusicProject` model and the migration **describe the same table**.
  The model's DDL was rendered and diffed against the migration's DDL,
  column by column, and they match.
- `guardian/routes/music_projects.py` imports cleanly against the real
  `guardian` package and produces an OpenAPI schema with no parameter
  demoted to a query string.
- The `PgDB` methods **run against a live Postgres**: upsert, read, list,
  delete, the complete-state write (an omitted value is cleared, not
  merged), and the foreign key actually rejecting an orphan.
- The route **has been exercised against a live Agent-DAW service**, over
  real HTTP, with the real model in the loop: `compose` returns a job
  handle, the job succeeds, the job comes back carrying the project and
  thread it was started with, and the produced files are registered as
  `MediaAsset` rows in a live database.
- The bind-mount **delivers**: rendering through the container puts the
  stems, the MIDI and both REAPER projects in the host's directory, where
  the DAW can open them.
- 22 new tests pass, and the supported-profile, command-bus, config and
  architecture suites still pass.

**Not verified:**

- The `PgDB` methods were exercised against a disposable Postgres, not
  against the host's own database.
- `MediaAsset` registration was verified end to end with the host's
  `media_identity` service and its own model, but the row has not been
  read back through the media library UI.

## The latency constraint that shapes the host integration

A live compose takes **~107 seconds**, and the render itself is only
~2 seconds -- the Producer's LLM call is the long pole. That was
measured against the thinking budget to check whether it was tunable:

| Thinking budget | Compose latency |
|---|---|
| 4000 (default) | ~107s |
| 1024 | 137s |
| 256 | 159s |

No correlation. The latency is inherent to the model on this gateway and
cannot be tuned away.

Two consequences:

1. Codexify's plugin facade allows **10 seconds** per invoke, so the
   service-plugin path must be asynchronous. `agentdaw/serve.py` has a
   job API for exactly this.
2. On the route path, a chat turn that blocks for ~107 seconds is a turn
   the Artist watches hang. The honest shape is the same: start the job,
   return a handle, report back when it lands.

The fast operations -- `preview`, `render`, `measure`, `anchor` -- are
sub-second to two-second, and they are the ones the audition loop
actually uses.
