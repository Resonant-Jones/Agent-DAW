# The Codexify-side patch

This directory holds the Codexify half of the integration, staged here so
it can be reviewed before it touches Codexify's repository.

It is **not** part of the `agentdaw` package. Nothing in `agentdaw/`
imports anything here.

The patches below are snapshots of PR diffs, so they come in two
states: some of them have been merged into Codexify and are kept here as
a record of what landed, and some are open proposals. The table in
"What is here" says which is which.

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
| `music-projects.patch` | The unified diff of both merged PRs, against existing Codexify files. Applies onto `74fd8f48` |
| `music-registration-fix.patch` | The unified diff of PR #1003 -- the artifact-registration fix, not yet merged. Applies onto `68c9e523` |
| `adr-108-authority-pointer.patch` | The unified diff of PR #1004 -- the ADR-108 authority pointer, not yet merged. Applies onto `68c9e523` |
| `music-engine-pin.patch` | The unified diff of PR #1005 -- the engine pin, not yet merged. Applies onto `c28bc8f4` |
| `guardian/routes/music_projects.py` | New: the route, the forwarding, and the artifact registration |
| `guardian/db/migrations/versions/a7d4c9e2f1b3_add_music_projects_table.py` | New: the Alembic migration |
| `config/supported_profiles/v1-music-producer.yaml` | New: a supported profile that enables the route |
| `plugins/agentdaw/manifest.json` | New: what Codexify advertises the service as |
| `guardian/skills/music-producer/SKILL.md` | New: the conduct |
| `tests/routes/test_music_projects.py` | New: the tests |

The compose service and the `guardian_api.py` mount are in the patch.

## How to apply it

**Each patch applies onto one specific base commit.** The base matters,
and it is not "whatever checkout you have" -- these are snapshots of PR
diffs, so the tree they were cut against is part of the artifact:

| Patch | Base | Why that base |
|---|---|---|
| `music-projects.patch` | `74fd8f48` | the commit before `69ef035a`, which is what PR #988 was cut from |
| `music-registration-fix.patch` | `68c9e523` | the commit PR #1003 was cut from |
| `adr-108-authority-pointer.patch` | `68c9e523` | the commit PR #1004 was cut from |
| `music-engine-pin.patch` | `c28bc8f4` | the commit PR #1005 was cut from |

Those bases were checked with `git apply --check` against clean
checkouts of each commit, not assumed. `music-projects.patch` applies
onto `74fd8f48` and onto nothing else; the two at `68c9e523` also
apply onto `40c96c8f` (its parent, where the same files are
unchanged); `music-engine-pin.patch` applies onto `c28bc8f4`.

To apply the whole thing onto the state before any of the music work
landed:

```bash
git checkout 74fd8f48
git apply --check codexify/music-projects.patch   # dry run
git apply codexify/music-projects.patch
```

The patch creates the new files itself -- the route, the migration, the
supported profile, the plugin manifest, the skill and the tests -- so
there is nothing to copy afterwards.

The artifact-registration fix and the ADR-108 pointer are separate PRs,
cut from a later base, so they go on top of `68c9e523` rather than on
top of `music-projects.patch`:

```bash
git checkout 68c9e523
git apply codexify/music-registration-fix.patch
git apply codexify/adr-108-authority-pointer.patch
```

They apply independently of each other.

The engine pin is the last of them, and it is the one that makes the
whole thing work end to end:

```bash
git checkout c28bc8f4
git apply codexify/music-engine-pin.patch
```

**On today's Codexify `main` these patches are history.** #988, #997
and #1004 are merged, so `music-projects.patch` will refuse to apply
(`already exists in working directory`, and the edits to
`docker-compose.yml`, `guardian/core/pgdb.py` and
`guardian/guardian_api.py` are already there). To read what landed,
use the history rather than the patch:

```bash
git log --oneline --grep=music
```

Once PR #1003 merges, `music-registration-fix.patch` will fail the same
way, because its change is in the tree. Once PR #1005 merges,
`music-engine-pin.patch` will fail the same way.

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

## What the version numbers mean

There is one version, and it is written in several places so that it can
be checked: the packaging metadata (`pyproject.toml`), the package
itself (`agentdaw.__version__`), the service (`SERVICE_VERSION`, which
is what `GET /health` reports), and the plugin manifest -- in both the
packaged copy and the staged mirror. A test asserts that all of them
agree, because a host told the plugin is one version while the service
reports another has no way to tell which is the real one.

The git tags track it: `v0.1.0` through `v0.1.6`.

The rule for when a change gets a release: a change that alters what
the service *does* gets a tag and moves the version. A change that only
stages or documents gets neither, and `main` runs ahead of the last
tag. So the version names the release the code corresponds to, not the
commit it happens to sit on.

Two things in Codexify move with it and have to be moved together:

- the compose service's build context, `.../Agent-DAW.git#v0.1.6`
- `plugins/agentdaw/manifest.json`

The compose pin is not a formality. It names the release that was
qualified against the live route, and the route's async path depends on
the engine carrying the job's context, which only arrived in v0.1.3 --
see the section below. A newer release is not pinned until it has been
qualified.

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
owns: the path, the media kind, the mime type and the byte count.

**The content hash is the host's too.** The Producer reports a hash for
each file it produced, and the host reads the bytes anyway -- so the
host hashes them itself and treats the Producer's hash as a claim. If
they disagree, the bytes win and the disagreement is logged. Taking the
Producer's hash as the identity would let a wrong hash become the row's
identity, and two registrations of the same file would then become two
rows.

This was one of three defects found by running the whole chain against a
live database rather than a mock -- see
[`music-registration-fix.patch`](#the-one-bug-the-review-could-not-see)
below.

The hash is of the bytes *actually written to disk*, not of what was
rendered in memory -- otherwise the registry would index something the
Artist never heard.

Verified end to end against the real checkout: a render produces seven
artifacts, each one becomes a `MediaAsset` row whose NOT NULL columns
and CHECK constraints are satisfied, and audio lands under the `audio/`
storage prefix:

```
deterministic_id : 20261030-7d126d8f
normalized_slug  : pad
system_name      : 20261030-7d126d8f--pad.wav
storage_prefix   : audio/
```

## The engine version is part of the contract

A compose is a job, and the job outlives the request that started it.
When the host polls for the result it has to remember what it asked
for -- which project, which thread -- so it can attribute the files it
gets back. The host sends that context with the request, and the engine
carries it on the job. The route then reads it back:

```python
# guardian/routes/music_projects.py
raw_project_id = (result.get("context") or {}).get("project_id")
if not raw_project_id:
    logger.warning("[music_projects] files were produced without a "
                   "project; they will not be registered")
    return
```

That echo is the engine's, and it is why the tag in the compose service
matters:

| Engine | The job echoes its context |
|---|---|
| v0.1.2 | no |
| v0.1.3 and later | yes |

With a v0.1.2 engine the files are produced, the registration is
skipped, and the route still reports success -- the same silent failure
PR #1003 fixes, one layer up. This was measured on the images built
from the tags themselves, the way compose builds them:

```
$ docker build https://github.com/Resonant-Jones/Agent-DAW.git#v0.1.2
$ curl -s -X POST localhost:8002/invoke -d '{...,"context":{"project_id":"999"}}'
$ curl -s localhost:8002/jobs/$JOB
  {"status": "failed", "context": null, ...}
```

The check needs no model call and no render -- a failed job still
carries the context, so a missing one is unambiguous.

**The host should not be depending on this.** It sent the context, so
it could keep its own copy keyed by job id. That removes the coupling
between the host's correctness and the engine's age. It is not done
yet.

## The one bug the review could not see

PR #988 merged with 24 passing tests and an OpenAPI schema that
described the surface correctly. Then the whole chain was run against a
live Postgres and a live Agent-DAW service, and:

```
[FAIL] MediaAsset rows were created -- 1
[FAIL] every artifact was registered -- 1 of 7
```

**One** of the seven files a render produces was registered, and the
route reported success. Three defects, all silent:

1. `asset.id` was read *after* the session that committed the row had
   closed. Committing expires the instance's attributes, and a closed
   session cannot refresh them, so the read raised
   `DetachedInstanceError` -- on the first artifact, every time. The
   exception was swallowed by the route's log-and-continue.
2. The loop had no per-artifact isolation, so one failure aborted every
   artifact after it.
3. Registration was not idempotent. A finished job can be polled more
   than once, and the second poll collided with the unique index
   `uq_media_assets_active_identity`, taking the rest of the session
   with it.

The test that was supposed to guard this path used a **fake session**:

```python
class _Session:
    def add(self, _asset): calls.append("add")
    def flush(self): calls.append("flush")
    def commit(self): calls.append("commit")
```

A fake session has no attribute expiry, no commit semantics and no
unique constraint. It asserted that `commit` was *called* -- and
`commit` was called. The bug was three lines later.

ADR-108 requires the artifact-custody assertion to be proven against a
real database, so the replacement tests use a **disposable PostgreSQL**.
Reverting only the route to `main`, with the new tests unchanged, fails
all five:

```
FAILED test_a_registration_that_does_not_commit_is_not_a_registration
FAILED test_every_produced_file_is_registered_not_just_the_first
FAILED test_registering_the_same_file_twice_is_one_row
FAILED test_the_host_hashes_the_bytes_rather_than_trusting_the_producer
FAILED test_artifacts_are_registered_against_a_project_with_no_thread
```

With the fix, the same live chain reports **7 of 7 registered, 0 failing
checks**. That is PR #1003.

## The authority boundary, and where it is recorded

Codexify's own `docs/architecture/adr/108-codexify-music-producer-authority-and-agentdaw-service-boundary.md`
records the four-party split:

| Party | Owns |
|---|---|
| Codexify | identity, project containment, the durable record of musical state, artifact custody, the permission gate |
| Agent-DAW | the specification and the render |
| REAPER | DAW and timeline state, in the handoff artifact |
| the Artist | intent, locked values, acceptance |

Two things in it are worth repeating here because they are easy to get
backwards:

- **`current_spec` is a record, not a second truth.** The Producer owns the
  specification; the column is what Codexify keeps of it. It is written
  whole and never field-patched, so a revision replaces the whole state.
- **Codexify owns no normalized composition schema.** An earlier draft
  proposed `studio_arrangements` / `studio_tracks` tables in Codexify
  Postgres. That was withdrawn as a duplicate truth plane over
  Agent-DAW's published specification contract. The composition truth
  already exists, and Agent-DAW owns it.

ADR-108's follow-through list asked for `docs/Future-Features/PLAN.md` to
point at the ADR, "so the plan and the ADR do not drift" -- the Studio v1
plan predates this surface and read alone it implies Codexify owns the
DAW-side session model. PR #1004 adds that pointer and a proof receipt
that records the state of all five follow-through items.

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

`current_spec` holds the complete session state as the Producer produced
it, and it is a *record* of that state rather than a second truth about
it: the Producer owns the specification, and this column is what this
side keeps of it. It is written whole and never field-patched, so a
revision replaces the whole state. The scalar columns beside it (`bpm`,
`musical_key`, `bars`, ...) are a queryable projection of the same values
-- "every project at 120 BPM" should not mean parsing JSON. They are
deliberately not indexed: the table is small and is almost always reached
by `project_id`.

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
- The whole-state write **replaces** rather than merges, against a live
  database: a partial write clears the columns it omits. The test fails if
  the upsert is given merge semantics.
- 24 new tests pass, and the supported-profile, command-bus, config and
  architecture suites still pass.
- With PR #1003 applied, 28 tests pass in the route suite (24 original
  plus 5 new, minus the one mock-based test they replaced) and the
  media-route suites still pass -- 64 tests across `test_music_projects.py`,
  `test_media_routes.py`, `test_media_upload.py` and `test_media_identity.py`.
  The live chain against a live Postgres and a live Agent-DAW service
  registers **7 of 7** artifacts, 0 failing checks.
- The engine's context echo was measured on images **built from the git
  tags the way compose builds them** -- `v0.1.2` answers `"context": null`,
  `v0.1.5` answers with the context it was given. No model call and no
  render are needed to see it.

**Not verified:**

- The `PgDB` methods were exercised against a disposable Postgres, not
  against the host's own database.
- `MediaAsset` registration was verified end to end with the host's
  `media_identity` service and its own model, but the row has not been
  read back through the media library UI.
- REAPER's unattended render is blocked on licensing, so "the project
  opens in REAPER" is proven and "REAPER renders it unattended" is not.
  The Codexify integration does not depend on either.

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
