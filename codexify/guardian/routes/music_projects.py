"""
Music Project Routes
~~~~~~~~~~~~~~~~~~~~~

A Music Project is an ordinary Codexify ``Project`` with musical state
attached to it. There is no parallel chat concept: threads and chats are
the existing ``ChatThread`` / ``ChatMessage`` rows, which already carry
a ``project_id`` foreign key. What this module adds is the ability to
read and write the musical state, and to mount the Producer's own routes
so a thread can call them.

The state endpoints here are deliberately thin. The Producer owns the
music; this side owns the record of it. Everything that decides *what*
the music should be -- tempo, key, bars, the spec itself -- is derived
by the Producer and written back complete, never patched field by field,
because a revision replaces the whole state.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

# Shared dependencies are imported defensively, the way guardian/routes/
# projects.py does it: guardian_api.py imports this module at module
# scope, so an ImportError here would stop the whole app from booting.
try:
    from guardian.core.dependencies import (
        RequestUserScope,
        chatlog_db,
        get_account_user_scope as get_request_user_scope,
    )
except ImportError:  # pragma: no cover - dependency shape guard
    chatlog_db = None
    RequestUserScope = object  # type: ignore[assignment,misc]

    def get_request_user_scope():  # type: ignore[misc]
        return None


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/music", tags=["music"])


class MusicProjectState(BaseModel):
    """The musical state of a Music Project.

    ``current_spec`` is the complete session state. It is written whole,
    never merged: a revision is a new complete state, and a partial
    update here would silently resurrect values the Artist already
    changed.
    """

    bpm: Optional[float] = None
    musical_key: Optional[str] = None
    musical_mode: Optional[str] = None
    bars: Optional[int] = None
    time_signature: Optional[str] = None
    length_seconds: Optional[float] = None
    library_path: Optional[str] = None
    current_spec: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="ignore")


class MusicProjectView(BaseModel):
    """A Music Project as the host sees it."""

    project_id: int
    bpm: Optional[float] = None
    musical_key: Optional[str] = None
    musical_mode: Optional[str] = None
    bars: Optional[int] = None
    time_signature: Optional[str] = None
    length_seconds: Optional[float] = None
    library_path: Optional[str] = None
    current_spec: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _require_db():
    if chatlog_db is None:
        raise HTTPException(
            status_code=503,
            detail="database is not configured",
        )
    return chatlog_db


def _serialise(row: Dict[str, Any]) -> Dict[str, Any]:
    """Make a database row safe to serialise as JSON.

    Timestamps come back as datetimes and the spec as a JSON string in
    some drivers; both need normalising before they can cross the wire.
    """
    payload = dict(row)
    for key in ("created_at", "updated_at"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            payload[key] = value.isoformat()
    spec = payload.get("current_spec")
    if isinstance(spec, str):
        import json

        try:
            payload["current_spec"] = json.loads(spec)
        except ValueError:
            payload["current_spec"] = None
    return payload


@router.get("/projects")
def list_music_projects(
    request_user_scope: RequestUserScope = Depends(get_request_user_scope),
):
    """Return every Music Project's musical state."""
    db = _require_db()
    try:
        rows = db.list_music_projects()
    except Exception as exc:
        logger.warning("[music_projects] failed to list music projects: %s", exc)
        raise HTTPException(status_code=500, detail="failed to list music projects")
    return [_serialise(row) for row in rows]


@router.get("/projects/{project_id}")
def get_music_project(
    project_id: int,
    request_user_scope: RequestUserScope = Depends(get_request_user_scope),
):
    """Return one Music Project's musical state.

    A project that has no musical state is not an error: it is an
    ordinary Project that has not been made into a Music Project yet.
    """
    db = _require_db()
    try:
        row = db.get_music_project(project_id)
    except Exception as exc:
        logger.warning(
            "[music_projects] failed to read music project %s: %s", project_id, exc
        )
        raise HTTPException(status_code=500, detail="failed to read music project")
    if row is None:
        raise HTTPException(status_code=404, detail="not a music project")
    return _serialise(row)


@router.put("/projects/{project_id}")
def put_music_project(
    project_id: int,
    body: MusicProjectState = Body(...),
    request_user_scope: RequestUserScope = Depends(get_request_user_scope),
):
    """Attach or replace a Music Project's musical state.

    The write is complete, not incremental: the values in ``body`` are
    the state, and anything omitted becomes null. That is what makes a
    revision a revision rather than an edit.
    """
    db = _require_db()
    try:
        row = db.upsert_music_project(
            project_id,
            bpm=body.bpm,
            musical_key=body.musical_key,
            musical_mode=body.musical_mode,
            bars=body.bars,
            time_signature=body.time_signature,
            length_seconds=body.length_seconds,
            library_path=body.library_path,
            current_spec=body.current_spec,
        )
    except Exception as exc:
        logger.warning(
            "[music_projects] failed to write music project %s: %s", project_id, exc
        )
        raise HTTPException(status_code=500, detail="failed to write music project")
    return _serialise(row)


@router.delete("/projects/{project_id}")
def delete_music_project(
    project_id: int,
    request_user_scope: RequestUserScope = Depends(get_request_user_scope),
):
    """Detach the musical state. The Project itself is untouched."""
    db = _require_db()
    try:
        removed = db.delete_music_project(project_id)
    except Exception as exc:
        logger.warning(
            "[music_projects] failed to delete music project %s: %s", project_id, exc
        )
        raise HTTPException(status_code=500, detail="failed to delete music project")
    if not removed:
        raise HTTPException(status_code=404, detail="not a music project")
    return {"project_id": project_id, "deleted": True}


def build_producer_router():
    """Build Agent-DAW's producer routes.

    Imported lazily so Agent-DAW stays an optional extra: a host that
    has not installed it should still boot. The caller decides what to
    do when this raises -- the routes will simply be absent from the
    OpenAPI schema, and therefore from the model's tool list.
    """
    from agentdaw.codexify.router import build_router

    return build_router(prefix="/api/music")
