"""A FastAPI router exposing Agent-DAW to Codexify.

Codexify derives its agent-callable tools from the OpenAPI schema
(``guardian/command_bus/manifest.py`` -> ``build_manifest``), so mounting
these routes is exactly what makes the Producer reachable by a model.
Three things follow from how Codexify works, and they are the whole
reason this adapter lives here rather than in Codexify itself:

* **The route label must exist in the supported profile.** Codexify
  quarantines any router whose label is not listed in
  ``config/supported_profiles/*.yaml``. A new label needs a YAML entry
  or the routes are mounted but never advertised.
* **Routes that are hidden from the schema are hidden from the tools.**
  A route marked ``internal_only`` is deliberately popped out of
  ``app.openapi()``, so it never reaches the command-bus manifest. The
  Producer's routes are meant to be callable, so they are not.
* **The schema is the model's only documentation.** Every field name and
  docstring below is read by the model before it calls the route. That
  is why the request body is called ``brief`` and not ``params``: the
  model has to understand that it is handing over the Artist's words.

FastAPI is an optional dependency. Importing ``agentdaw`` must not
require it, so the import happens inside :func:`build_router`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import service

DEFAULT_LIBRARY = Path.home() / ".agentdaw" / "patch-library.json"


def _library_path(library: str | None) -> str | None:
    """The library to *read* from. None means there is nothing to read yet."""
    if library:
        return library
    if DEFAULT_LIBRARY.exists():
        return str(DEFAULT_LIBRARY)
    return None


def _library_save_path(library: str | None) -> str:
    """The library to *write* to.

    Approving a patch must work on a machine that has never approved one
    before, so the write target is the default path even when no file
    exists there yet. Reading and writing therefore have different
    answers, and conflating them made approve a silent no-op on a fresh
    install.
    """
    return library or str(DEFAULT_LIBRARY)


class ComposeRequest(BaseModel):
    """The Artist's brief, in the Artist's words.

    Describe the music you want: mood, tempo, instrumentation, how it
    should move, how long it should be. Tempo, key, bar count and
    length stated here are treated as locked and will not be
    reinterpreted. Do not pass DSP values -- the engine owns those.
    """

    brief: str = Field(
        ...,
        description="The artist's description, in plain words. Not DSP parameters.",
    )
    library: str | None = Field(
        default=None, description="Path to the approved patch library JSON."
    )
    render: bool = Field(
        default=True, description="Also render stems, MIDI and the project."
    )
    out_dir: str | None = Field(
        default=None,
        description=(
            "Directory to write artifacts into. The host controls this so the "
            "rendered files land where its own registry can see them."
        ),
    )


class ReviseRequest(BaseModel):
    """A correction to an existing session."""

    spec: dict[str, Any] = Field(..., description="The existing session spec.")
    feedback: str = Field(
        ..., description="What the artist wants changed, in plain words."
    )
    library: str | None = None


class PreviewRequest(BaseModel):
    """A fast audition of one patch."""

    spec: dict[str, Any] = Field(..., description="The session spec.")
    patch_id: str = Field(..., description="Which patch to audition.")


class RenderRequest(BaseModel):
    """A full render of a spec."""

    spec: dict[str, Any] = Field(..., description="The session spec.")
    out_dir: str | None = None


class ApproveRequest(BaseModel):
    """Promote a session's patches into the approved library."""

    spec: dict[str, Any] = Field(..., description="The session spec.")
    measurements: dict[str, dict[str, float]] | None = Field(
        default=None,
        description="Per-track measurements, keyed by track id.",
    )
    library: str | None = None


def build_router(prefix: str = "/api/music"):
    """Build the Agent-DAW router. Import FastAPI lazily.

    Returns a FastAPI ``APIRouter``. The caller mounts it the way
    Codexify mounts its own routers, through ``_include_router`` with a
    label that exists in the active supported profile.

    The request models live at module scope, not inside this function, and
    that is load-bearing rather than stylistic. This module uses
    ``from __future__ import annotations``, so every annotation is a
    *string* at runtime; FastAPI resolves those strings against the
    defining module's globals. A model defined inside a function is not
    in those globals, so the annotation cannot be resolved, FastAPI falls
    back to treating the argument as a query parameter, and every request
    comes back 422. The symptom is silent -- the route exists, the
    OpenAPI schema is wrong, and nothing tells you why.
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix=prefix, tags=["music"])

    @router.post(
        "/compose",
        summary="Produce a music session from an artist's description",
        description=(
            "Turn a description into a session: a spec, rendered stems, MIDI, a mix "
            "and a REAPER project, plus the measurements that were taken of them. "
            "Approved patches are retrieved from the library and revised rather "
            "than reinvented when one matches."
        ),
    )
    def compose(req: ComposeRequest) -> dict[str, Any]:
        try:
            return service.compose_session(
                req.brief,
                library=_library_path(req.library),
                out_dir=req.out_dir,
                render=req.render,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post(
        "/revise",
        summary="Revise a session from the artist's correction",
        description=(
            "Apply a correction in plain words. Everything the artist did not "
            "mention stays as it was; the returned spec is a complete new state."
        ),
    )
    def revise(req: ReviseRequest) -> dict[str, Any]:
        try:
            return service.revise_session(
                req.spec, req.feedback, library=_library_path(req.library)
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post(
        "/preview",
        summary="Audition one patch quickly",
        description=(
            "Render one patch under the session's drum bed. Fast on purpose: this "
            "is the loop the artist approves a sound in. Returns measurements, "
            "not adjectives."
        ),
    )
    def preview(req: PreviewRequest) -> dict[str, Any]:
        try:
            return service.preview_patch(req.spec, req.patch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.post(
        "/render",
        summary="Render a spec to stems, MIDI, a mix and a project",
    )
    def render(req: RenderRequest) -> dict[str, Any]:
        try:
            return service.render_session_artifacts(
                req.spec, out_dir=req.out_dir or "out"
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get(
        "/anchors",
        summary="Find approved patches that match a request",
        description=(
            "Search the approved patch library. A miss is a legitimate answer and "
            "is reported as one, so the artist can be told that nothing matches "
            "instead of being handed a false recall."
        ),
    )
    def anchors(
        query: str, library: str | None = None, limit: int = 5
    ) -> dict[str, Any]:
        return service.retrieve_anchors(
            query, library=_library_path(library), limit=limit
        )

    @router.post(
        "/approve",
        summary="Promote a session's patches into the approved library",
    )
    def approve(req: ApproveRequest) -> dict[str, Any]:
        try:
            return service.approve_patches(
                req.spec,
                measurements=req.measurements,
                library=_library_path(req.library),
                library_path=_library_save_path(req.library),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
