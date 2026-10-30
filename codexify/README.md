# The Codexify-side patch

This directory holds the Codexify half of the integration, staged here so
it can be reviewed before it touches Codexify's repository.

It is **not** part of the `agentdaw` package. Nothing in `agentdaw/`
imports anything here.

## What is here

| Path | What it is |
|---|---|
| `music-projects.patch` | A unified diff against four existing Codexify files |
| `guardian/routes/music_projects.py` | New file: the state endpoints and the artifact registration |
| `guardian/db/migrations/versions/a7d4c9e2f1b3_add_music_projects_table.py` | New file: the Alembic migration |

## The diff touches four files

| File | Change |
|---|---|
| `guardian/db/models.py` | Adds the `MusicProject` model |
| `guardian/core/pgdb.py` | Adds `get/upsert/delete/list_music_project` to `PgDB` |
| `guardian/guardian_api.py` | Mounts the router under the `music_projects` label |
| `config/supported_profiles/v1-local-core-web-mcp.yaml` | Adds `music_projects` to `route_posture.enabled` |

That last one is not optional. Codexify **quarantines** any router whose
label is not listed in the active supported profile
(`guardian/core/supported_profile.py:38-46`). A missing label means the
routes mount but never reach the tool manifest, and the Producer is
silently uncallable.

## How to apply it

From the root of a Codexify checkout:

```bash
git apply --check codexify/music-projects.patch   # dry run
git apply codexify/music-projects.patch
cp codexify/guardian/routes/music_projects.py guardian/routes/
cp codexify/guardian/db/migrations/versions/a7d4c9e2f1b3_add_music_projects_table.py \
   guardian/db/migrations/versions/
```

Then the skill, and then the router label.

## What has been verified, and what has not

**Verified, against the real Codexify checkout:**

- The migration loads in Alembic and leaves **exactly one head**, with
  `a7d4c9e2f1b3` directly descending from the merge head `3a91d7c4e8b2`.
  (`alembic heads` reported 31 candidate heads when the revision graph
  was parsed by regex; Alembic itself reports one. The regex was wrong,
  not the chain.)
- The migration renders to valid Postgres DDL in Alembic's offline mode,
  including `PRIMARY KEY (project_id)` and
  `FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE`.
- The `MusicProject` model and the migration **describe the same table**.
  The model's DDL was rendered and diffed against the migration's DDL,
  column by column, and they match.
- `guardian/routes/music_projects.py` imports cleanly against the real
  `guardian` package, mounts the four expected paths, and produces an
  OpenAPI schema with no parameter demoted to a query string.
- The profile YAML parses and `music_projects` is present in
  `route_posture.enabled`.
- Every rendered artifact becomes a `MediaAsset` row that satisfies the
  model's NOT NULL columns and CHECK constraints, with the identity
  fields computed by Codexify's own `media_identity` service.

**Not verified:**

- The patch has not been applied to a Codexify checkout, so it has not
  been run against Codexify's own test suite. Its own CI is the real
  gate.
- The `PgDB` methods have not been executed against a live Postgres. The
  SQL follows the surrounding methods' idiom, but "follows the idiom" is
  not "runs".
- `agentdaw` is not yet importable inside the Codexify backend image.
  Five of its six dependencies are already in Codexify's locked
  `backend/requirements.txt`; only `httpx` is missing, and `httpx` is
  used solely by Agent-DAW's own LLM client. If Codexify supplies the
  inference, it is not needed at all.

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
`register_artifacts()` in `guardian/routes/music_projects.py` calls that
function rather than reimplementing it, so the rules cannot drift.
Agent-DAW supplies only the facts it owns: the path, the media kind, the
mime type, the byte count, and the content hash.

The content hash is computed by the Producer while the bytes are already
in hand, so the host does not read every stem back off disk just to
hash it. It is the hash of the bytes *actually written to disk*, not of
what was rendered in memory — otherwise the registry would index
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
