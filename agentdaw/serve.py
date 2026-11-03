"""Agent-DAW as a Codexify service plugin.

Codexify has two extension seams and this module speaks the second one: a
service plugin. The contract is the canonical service-plugin protocol v1.0,
which Codexify's own TTS service already implements:

* ``GET  {base_url}/health`` -> a readiness snapshot.
* ``POST {base_url}/invoke`` -> the canonical envelope in, the canonical
  response out.

The envelope is::

    {"protocol_version": "1.0",
     "plugin_id": "agentdaw",
     "capability": "music",
     "action": "compose",
     "input": {...},
     "context": {"request_id": ..., "thread_id": ..., "user_id": ...}}

and the response is::

    {"ok": true,  "output": {...}, "error": null}
    {"ok": false, "output": null, "error": {"code": ..., "message": ..., "retryable": bool}}

Codexify's facade (``guardian/core/plugins.py``) validates that ``ok`` is a
boolean and raises on anything else, so the shape above is not negotiable.

**Why the job API exists.** Codexify's plugin facade has a hard 10-second
invoke timeout, and a live compose takes far longer than that: the Producer's
LLM call is the long pole (measured at ~107s for one spec), while the render
itself is only ~2s. A synchronous ``compose`` would therefore be killed by
the facade mid-flight. So ``input.mode = "async"`` returns a job handle
immediately and the host polls ``GET /jobs/{job_id}``. The ``render``,
``preview``, ``measure`` and ``anchor`` actions are all fast enough to stay
synchronous.

The threading here is a thread pool, not an event loop, because the DSP is
numpy work that releases the GIL: a render genuinely runs on another core
while the service keeps answering health checks.

FastAPI is an optional dependency. Importing ``agentdaw`` must not require
it, so the import happens inside :func:`build_app`.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import service

__all__ = [
    "CAPABILITY",
    "ACTIONS",
    "build_app",
    "create_app",
]

CAPABILITY = "music"

#: The actions this service answers. Codexify advertises them from the
#: plugin manifest, so the two must agree exactly.
ACTIONS = (
    "compose",
    "revise",
    "preview",
    "render",
    "measure",
    "anchor",
    "approve",
    "job_status",
)

PROTOCOL_VERSION = "1.0"
SERVICE_NAME = "Agent-DAW"
SERVICE_VERSION = "0.1.0"

#: Where rendered artifacts go.
#:
#: This matters more than it looks. When the service runs in a container
#: and the DAW runs on the host, the files have to be written into a
#: directory that is a bind-mount, or the host cannot open them. The
#: environment variable is how the operator points the service at that
#: mount without rebuilding the image; the image sets it to the mount
#: path, and a local run falls back to a relative directory.
ENV_OUT_ROOT = "AGENTDAW_OUT_ROOT"

DEFAULT_OUT_ROOT = Path(os.environ.get(ENV_OUT_ROOT) or "out")


# --------------------------------------------------------------------------
# the canonical protocol's shapes
#
# These are module-level on purpose. This module uses
# ``from __future__ import annotations``, so annotations are strings at
# runtime and FastAPI resolves them against the module globals. A model
# defined inside a function cannot be resolved there, so FastAPI silently
# demotes the argument to a query parameter and every request 422s.
# --------------------------------------------------------------------------

class PluginInvokeErrorModel(BaseModel):
    code: str
    message: str
    retryable: bool = False


class PluginInvokeResponse(BaseModel):
    ok: bool
    output: dict | None = None
    error: PluginInvokeErrorModel | None = None


class PluginInvokeContext(BaseModel):
    request_id: str | None = None
    thread_id: str | None = None
    user_id: str | None = None


class PluginInvokeRequest(BaseModel):
    protocol_version: str
    plugin_id: str
    capability: str
    action: str
    input: dict = Field(default_factory=dict)
    context: PluginInvokeContext | None = None


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


class _Job:
    """One asynchronous unit of work."""

    __slots__ = ("id", "action", "status", "output", "error", "created_at", "finished_at")

    def __init__(self, action: str) -> None:
        self.id = uuid.uuid4().hex
        self.action = action
        self.status = "running"
        self.output: dict[str, Any] | None = None
        self.error: dict[str, Any] | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "action": self.action,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class _JobStore:
    """In-memory job registry. Bounded so a long-lived service cannot leak."""

    def __init__(self, max_jobs: int = 64) -> None:
        self._jobs: dict[str, _Job] = {}
        self._order: list[str] = []
        self._max = max_jobs
        self._lock = threading.Lock()

    def create(self, action: str) -> _Job:
        job = _Job(action)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._max:
                oldest = self._order.pop(0)
                self._jobs.pop(oldest, None)
        return job

    def get(self, job_id: str) -> _Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def finish(self, job: _Job, output: dict[str, Any] | None, error: dict[str, Any] | None) -> None:
        job.output = output
        job.error = error
        job.status = "failed" if error else "succeeded"
        job.finished_at = time.time()


#: The job registry is a module-level singleton so that the ``job_status``
#: action can reach it from the dispatcher, which has no access to the
#: closure the app is built in.
_JOBS = _JobStore()


# --------------------------------------------------------------------------
# action dispatch
# --------------------------------------------------------------------------


def _out_dir_for(context: dict[str, Any] | None, explicit: str | None) -> str:
    """Where this call's artifacts go.

    A host that passes a ``thread_id`` in the envelope wants the files to
    land somewhere it can attribute to that thread, so they are namespaced
    by it. Otherwise the host's explicit choice wins, and failing that a
    plain directory.
    """
    if explicit:
        return explicit
    thread_id = (context or {}).get("thread_id")
    if thread_id:
        return str(DEFAULT_OUT_ROOT / str(thread_id))
    return str(DEFAULT_OUT_ROOT)


class _UnsupportedAction(KeyError):
    """An action this service does not answer.

    A distinct subclass, because a ``KeyError`` raised *inside* a service
    call means something else entirely -- a missing patch, say -- and
    reporting that as "unsupported operation" would tell the host the
    wrong thing about its own request.
    """


def _dispatch(action: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Run one action and return its output.

    The inputs are the Artist's words and the session state. No DSP values
    cross this boundary: the engine owns those.
    """
    if action == "compose":
        return service.compose_session(
            str(payload.get("brief") or ""),
            library=payload.get("library"),
            out_dir=_out_dir_for(context, payload.get("out_dir")),
            model=payload.get("model"),
            render=bool(payload.get("render", True)),
        )

    if action == "revise":
        return service.revise_session(
            payload.get("spec") or {},
            str(payload.get("feedback") or ""),
            library=payload.get("library"),
            model=payload.get("model"),
        )

    if action == "preview":
        return service.preview_patch(
            payload.get("spec") or {},
            str(payload.get("patch_id") or ""),
            out_dir=_out_dir_for(context, payload.get("out_dir")),
        )

    if action == "render":
        return service.render_session_artifacts(
            payload.get("spec") or {},
            out_dir=_out_dir_for(context, payload.get("out_dir")),
        )

    if action == "measure":
        return service.measure_file(str(payload.get("path") or ""))

    if action == "anchor":
        return service.retrieve_anchors(
            str(payload.get("query") or ""),
            library=payload.get("library"),
            limit=int(payload.get("limit") or 5),
        )

    if action == "approve":
        return service.approve_patches(
            payload.get("spec") or {},
            measurements=payload.get("measurements"),
            library=payload.get("library"),
            library_path=payload.get("library_path") or payload.get("library"),
        )

    if action == "job_status":
        job = _JOBS.get(str(payload.get("job_id") or ""))
        if job is None:
            raise KeyError(f"no such job: {payload.get('job_id')}")
        return job.snapshot()

    raise _UnsupportedAction(action)


def _error(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "output": None,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------


def build_app(out_root: str | Path | None = None):
    """Build the Agent-DAW service. Import FastAPI lazily.

    The returned app serves the canonical plugin protocol on ``/invoke`` and
    ``/health``, and the plain Agent-DAW routes on ``/api/music`` so a host
    can drive the same operations directly when it prefers.
    """
    from fastapi import FastAPI, HTTPException

    if out_root is not None:
        global DEFAULT_OUT_ROOT
        DEFAULT_OUT_ROOT = Path(out_root)

    app = FastAPI(
        title=SERVICE_NAME,
        version=SERVICE_VERSION,
        description=(
            "An LLM studio producer. Describe a vision in words; get stems, MIDI, "
            "a mix, a REAPER project and the measurements that were taken of them."
        ),
    )

    jobs = _JOBS
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agentdaw")

    def _run_job(job: _Job, payload: dict[str, Any], context: dict[str, Any]) -> None:
        try:
            output = _dispatch(job.action, payload, context)
        except _UnsupportedAction as exc:
            jobs.finish(job, None, {"code": "unsupported_operation", "message": str(exc), "retryable": False})
        except KeyError as exc:
            jobs.finish(
                job,
                None,
                {"code": "not_found", "message": str(exc), "retryable": False},
            )
        except Exception as exc:  # noqa: BLE001
            jobs.finish(
                job,
                None,
                {
                    "code": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "retryable": False,
                },
            )
        else:
            jobs.finish(job, output, None)

    @app.get("/health")
    def health_check() -> dict[str, Any]:
        """Readiness snapshot, in the shape Codexify's facade expects."""
        return {
            "status": "ok",
            "ready": True,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "capability": CAPABILITY,
            "actions": list(ACTIONS),
            "protocol_version": PROTOCOL_VERSION,
        }

    @app.post("/invoke", response_model=PluginInvokeResponse)
    def invoke_plugin(request: PluginInvokeRequest) -> dict[str, Any]:
        """The canonical service-plugin invocation endpoint."""
        if request.protocol_version != PROTOCOL_VERSION:
            return _error(
                "unsupported_protocol_version",
                f"Unsupported protocol_version: {request.protocol_version}",
            )
        if request.capability != CAPABILITY:
            return _error(
                "unsupported_operation",
                f"Unsupported capability: {request.capability}",
            )
        if request.action not in ACTIONS:
            return _error(
                "unsupported_operation",
                f"Unsupported operation: {request.capability}.{request.action}",
            )

        context = (
            request.context.model_dump() if request.context is not None else {}
        )
        payload = dict(request.input or {})

        if str(payload.get("mode") or "sync").lower() == "async":
            job = jobs.create(request.action)
            pool.submit(_run_job, job, payload, context)
            return {
                "ok": True,
                "output": {"job_id": job.id, "status": job.status},
                "error": None,
            }

        try:
            output = _dispatch(request.action, payload, context)
        except _UnsupportedAction as exc:
            return _error("unsupported_operation", str(exc))
        except KeyError as exc:
            return _error("not_found", str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(
                "internal_error",
                f"{type(exc).__name__}: {exc}",
            )
        return {"ok": True, "output": output, "error": None}

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        """Poll an asynchronous job."""
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job.snapshot()

    # The plain Agent-DAW routes, for hosts that prefer them to the
    # envelope. Same operations, same contracts.
    from .codexify.router import build_router

    app.include_router(build_router())

    return app


def create_app(out_root: str | Path | None = None):
    """ASGI entry point: ``uvicorn agentdaw.serve:create_app --factory``."""
    return build_app(out_root)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
