"""Codexify adapter.

Two things live here:

``router``
    A FastAPI router over :mod:`agentdaw.service`. Codexify derives its
    agent-callable tools from the OpenAPI schema, so mounting these
    routes is what makes the Producer reachable by a model.

``skills/music-producer/SKILL.md``
    The conduct that goes with the capability: retrieve before inventing,
    measure don't opine, revise by delta, keep the Artist's stated tempo
    and key locked.

FastAPI is optional. Importing this package must not require it.
"""

from __future__ import annotations

__all__ = ["build_router"]


def build_router(prefix: str = "/api/music"):
    """Build the Agent-DAW router. Imported lazily so FastAPI stays optional."""
    from .router import build_router as _build_router

    return _build_router(prefix=prefix)
