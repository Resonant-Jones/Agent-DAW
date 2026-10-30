"""The service layer: Agent-DAW's operations, as plain data in and out.

This is the seam a host application mounts. Codexify is the first one
intended, but nothing here knows that: every function takes JSON-ish
input and returns JSON-ish output, so any host -- a FastAPI router, a
skill, a CLI, a chat backend -- can drive the Producer without importing
web machinery.

Two rules govern the surface.

**Every input is the Artist's words.** A caller passes a brief, not a
filter cutoff, not a reverb time. The DSP is the engine's business.

**Every output carries its evidence.** A returned session comes with the
measurements that were taken of it and the artifacts that were written.
The caller can register those artifacts in its own registry without
re-deriving anything, and the artist can see the numbers rather than a
claim.

Nothing here is agent-specific. The Codexify router in
``agentdaw/codexify/`` is a thin adapter over this module.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .analyze import analyze, report_findings
from .engine import collect_notes, render_preview, render_session
from .llm import LLMClient, load_llm_config
from .library import (
    LibraryEntry,
    PatchLibrary,
    Provenance,
    describe_patch,
)
from .producer import LLMProducer, StubProducer
from .spec import SessionSpec
from .wav import write_wav

__all__ = [
    "compose_session",
    "revise_session",
    "preview_patch",
    "render_session_artifacts",
    "measure_file",
    "retrieve_anchors",
    "approve_patches",
    "Artifact",
]


class Artifact(dict):
    """One produced file, described the way a host registry wants it.

    A dict subclass so it serialises without ceremony, but with named
    access for readability at the call site.
    """

    @property
    def path(self) -> str:
        return self["path"]

    @property
    def role(self) -> str:
        return self["role"]


def _content_hash(path: Path) -> str | None:
    """sha256 of a produced file.

    The host needs this to register the file in its own media registry,
    and it is cheaper to compute it here, while the bytes are already
    in hand, than to have the host read every stem back off disk.
    """
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _artifact(
    path: Path | None,
    role: str,
    media_kind: str,
    mime_type: str,
    track_id: str | None = None,
) -> dict[str, Any]:
    if path is None:
        return {}
    exists = path.exists()
    return {
        "role": role,
        "path": str(path),
        "media_kind": media_kind,
        "mime_type": mime_type,
        "provenance": "generated",
        "track_id": track_id,
        "bytes": path.stat().st_size if exists else 0,
        "content_hash": _content_hash(path) if exists else None,
    }


def _producer(model: str | None = None):
    """The live Producer if one is configured, otherwise the stub.

    Falling back to the stub keeps every operation callable in a test, on
    a machine with no credentials, or inside a host that has its own
    inference and only wants the DSP.
    """
    config = load_llm_config(model=model)
    if config.available:
        return LLMProducer(LLMClient(config)), True
    return StubProducer(), False


def _library(library: PatchLibrary | str | Path | None) -> PatchLibrary:
    if isinstance(library, PatchLibrary):
        return library
    if library and Path(library).exists():
        return PatchLibrary.load(library)
    return PatchLibrary()


def compose_session(
    brief: str,
    *,
    library: PatchLibrary | str | Path | None = None,
    out_dir: str | Path | None = None,
    model: str | None = None,
    sample_rate: int = 44100,
    bit_depth: int = 24,
    render: bool = True,
    producer: Any | None = None,
) -> dict[str, Any]:
    """Turn the Artist's brief into a session, its stems, and its project.

    This is the operation a chat turn calls. It returns the spec, the
    artifacts, the measurements and the findings -- everything a host
    needs to register the result and show it to the artist.

    `producer` injects a Producer, which is how a host that has its own
    inference supplies one, and how the tests stay deterministic.
    """
    started = time.perf_counter()
    if producer is None:
        producer, used_llm = _producer(model)
    else:
        used_llm = not isinstance(producer, StubProducer)
    lib = _library(library)

    spec = producer.compose(brief, patch_library=lib if lib.entries else None)

    result: dict[str, Any] = {
        "spec": spec.model_dump(),
        "brief": spec.brief.model_dump(),
        "used_llm": used_llm,
        "library_hits": [
            hit.summary() for hit in lib.retrieve(brief)
        ] if lib.entries else [],
    }

    if render:
        target = Path(out_dir or "out")
        report = render_session(
            spec,
            target,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
        )
        result["artifacts"] = _collect_artifacts(report)
        result["measurements"] = {
            t.track_id: t.measurements.to_dict() for t in report.tracks
        }
        result["findings"] = list(report.findings)
        result["mix_measurements"] = (
            analyze(_read_stem(report.mix_path), sample_rate).to_dict()
            if report.mix_path else {}
        )

    result["seconds"] = round(time.perf_counter() - started, 3)
    return result


def revise_session(
    spec: dict[str, Any] | SessionSpec,
    feedback: str,
    *,
    library: PatchLibrary | str | Path | None = None,
    model: str | None = None,
    producer: Any | None = None,
) -> dict[str, Any]:
    """Apply the Artist's correction to an existing session.

    The revision keeps everything the Artist did not mention. The
    returned spec is a complete new state, never a partial edit.
    """
    existing = (
        spec if isinstance(spec, SessionSpec) else SessionSpec.model_validate(spec)
    )
    if producer is None:
        producer, used_llm = _producer(model)
    else:
        used_llm = not isinstance(producer, StubProducer)
    lib = _library(library)

    revised = producer.compose(
        existing.title,
        patch_library=lib if lib.entries else None,
        existing=existing,
        feedback=feedback,
    )
    return {
        "spec": revised.model_dump(),
        "brief": revised.brief.model_dump(),
        "used_llm": used_llm,
        "delta": feedback,
    }


def preview_patch(
    spec: dict[str, Any] | SessionSpec,
    patch_id: str,
    *,
    out_dir: str | Path | None = None,
    sample_rate: int = 44100,
) -> dict[str, Any]:
    """Audition one patch under the session's drum bed.

    The audition is fast on purpose: it is the loop the artist uses to
    approve a sound, so it renders in seconds and answers with
    measurements rather than adjectives.
    """
    session = (
        spec if isinstance(spec, SessionSpec) else SessionSpec.model_validate(spec)
    )
    patch = session.patch(patch_id)
    if patch is None:
        raise KeyError(f"no such patch: {patch_id}")

    track = next((t for t in session.tracks if t.patch_id == patch_id), None)
    notes = collect_notes(track, session) if track else []

    audio = render_preview(
        patch,
        notes,
        bpm=session.bpm,
        bars=session.bars,
        sample_rate=sample_rate,
        with_drums=True,
        beats_per_bar=session.time_signature[0],
    )

    artifacts: list[dict[str, Any]] = []
    if out_dir:
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"preview_{patch_id}.wav"
        write_wav(path, audio, sample_rate, 24)
        artifacts.append(
            _artifact(path, "preview", "audio", "audio/wav", patch_id)
        )

    measurements = analyze(audio, sample_rate)
    return {
        "patch": patch.model_dump(),
        "measurements": measurements.to_dict(),
        "findings": report_findings(measurements, patch.name),
        "artifacts": artifacts,
    }


def render_session_artifacts(
    spec: dict[str, Any] | SessionSpec,
    *,
    out_dir: str | Path,
    sample_rate: int = 44100,
    bit_depth: int = 24,
    render_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Render a spec to stems, MIDI, a mix and a REAPER project."""
    session = (
        spec if isinstance(spec, SessionSpec) else SessionSpec.model_validate(spec)
    )
    report = render_session(
        session,
        out_dir,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        render_dir=render_dir,
    )
    return {
        "spec": session.model_dump(),
        "artifacts": _collect_artifacts(report),
        "measurements": {
            t.track_id: t.measurements.to_dict() for t in report.tracks
        },
        "findings": list(report.findings),
        "seconds": round(report.render_seconds, 3),
    }


def measure_file(path: str | Path, sample_rate: int = 44100) -> dict[str, Any]:
    """Measure a file on disk."""
    from .wav import read_wav

    audio, actual_rate = read_wav(path)
    measurements = analyze(audio, actual_rate)
    return {
        "path": str(path),
        "measurements": measurements.to_dict(),
        "findings": report_findings(measurements, Path(path).stem),
    }


def retrieve_anchors(
    query: str,
    *,
    library: PatchLibrary | str | Path | None = None,
    limit: int = 5,
    target_measurements: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Find the approved patches that match a request.

    A miss is a legitimate answer and is reported as one, so the caller
    can tell the artist "nothing in the library matches this" rather than
    inventing a recall.
    """
    lib = _library(library)
    hits = lib.retrieve(query, limit=limit, target_measurements=target_measurements)
    return {
        "query": query,
        "count": len(hits),
        "hits": [hit.summary() for hit in hits],
        "library_size": len(lib.entries),
    }


def approve_patches(
    spec: dict[str, Any] | SessionSpec,
    *,
    measurements: dict[str, dict[str, float]] | None = None,
    library: PatchLibrary | str | Path | None = None,
    library_path: str | Path | None = None,
) -> dict[str, Any]:
    """Promote a session's patches into the library as approved anchors.

    Measurements are attached when they are available, because they are
    what makes retrieval measured rather than verbal.
    """
    session = (
        spec if isinstance(spec, SessionSpec) else SessionSpec.model_validate(spec)
    )
    lib = _library(library)
    measurements = measurements or {}

    approved: list[dict[str, Any]] = []
    for patch in session.patches:
        track = next((t for t in session.tracks if t.patch_id == patch.id), None)
        raw = measurements.get(track.id if track else "", {})
        numeric = {
            k: float(v)
            for k, v in raw.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        entry = LibraryEntry(
            id=patch.id,
            name=patch.name,
            description=patch.description,
            patch=patch,
            anchors=describe_patch(patch, patch.name, patch.description),
            measurements=numeric,
            provenance=Provenance(
                source=f"session:{session.title}",
                brief=session.brief.goal,
                origin="new",
            ),
        )
        lib.add(entry)
        approved.append(entry.summary())

    if library_path:
        lib.save(library_path)

    return {
        "approved": approved,
        "library_size": len(lib.entries),
        "library_path": str(library_path) if library_path else None,
    }


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _collect_artifacts(report) -> list[dict[str, Any]]:
    """Describe every file the render produced, the way a host wants it."""
    artifacts: list[dict[str, Any]] = []

    for track in report.tracks:
        if track.stem_path:
            artifacts.append(
                _artifact(track.stem_path, "stem", "audio", "audio/wav", track.track_id)
            )
        if track.midi_path:
            artifacts.append(
                _artifact(
                    track.midi_path, "sequence", "other", "audio/midi", track.track_id
                )
            )

    if report.mix_path:
        artifacts.append(_artifact(report.mix_path, "mix", "audio", "audio/wav"))
    if report.project_path:
        artifacts.append(
            _artifact(
                report.project_path,
                "reaper_project",
                "other",
                "text/plain",
            )
        )
    if report.midi_project_path:
        artifacts.append(
            _artifact(
                report.midi_project_path,
                "reaper_project_sequencing",
                "other",
                "text/plain",
            )
        )

    return [a for a in artifacts if a]


def _read_stem(path: Path):
    from .wav import read_wav

    audio, _ = read_wav(path)
    return audio
