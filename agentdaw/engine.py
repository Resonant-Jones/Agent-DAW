"""Session rendering: spec in, stems + MIDI + REAPER project out.

This is the seam where the Producer's plan becomes audio you can hear and
files you can open somewhere else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .analyze import Measurements, analyze, report_findings
from .drums import render_drums
from .dsp import render_track
from .midi import write_midi
from .reaper import write_rpp
from .spec import MidiNote, PatchSpec, SessionSpec
from .wav import read_wav, write_wav


def _pan_gains(pan: float) -> tuple[float, float]:
    """Equal-power pan. pan in -1..1."""
    angle = (pan + 1.0) * (np.pi / 4.0)
    return float(np.cos(angle)), float(np.sin(angle))


def _db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _fit(audio: np.ndarray, total_samples: int) -> np.ndarray:
    """Force an audio buffer to exactly the session length.

    Every track in a session must be the same length or the mix will not
    add up. Renderers legitimately return a different length -- a synth
    with a long release tail runs past the end -- so trim or pad here
    rather than hoping each renderer agrees.
    """
    if audio.ndim == 1:
        audio = np.column_stack([audio, audio])
    if len(audio) >= total_samples:
        return audio[:total_samples]
    pad = np.zeros((total_samples - len(audio), audio.shape[1]))
    return np.vstack([audio, pad])


@dataclass
class TrackRender:
    track_id: str
    name: str
    kind: str
    stem_path: Path | None = None
    midi_path: Path | None = None
    measurements: Measurements = field(default_factory=Measurements)


@dataclass
class SessionRender:
    spec: SessionSpec
    tracks: list[TrackRender] = field(default_factory=list)
    mix_path: Path | None = None
    project_path: Path | None = None
    midi_project_path: Path | None = None
    findings: list[str] = field(default_factory=list)
    render_seconds: float = 0.0

    def summary(self) -> str:
        lines = [f"'{self.spec.title}'  {self.spec.bpm:.0f} BPM  {self.spec.key}  {self.spec.bars} bars"]
        for t in self.tracks:
            m = t.measurements
            lines.append(
                f"  {t.name:<18} {t.kind:<6} peak {m.peak_dbfs:>6.1f} dBFS   "
                f"LUFS {m.integrated_lufs:>6.1f}   centroid {m.spectral_centroid_hz:>6.0f} Hz"
            )
        if self.findings:
            lines.append("")
            lines.append("  measurements:")
            for f in self.findings:
                lines.append(f"    - {f}")
        lines.append("")
        lines.append(f"  rendered in {self.render_seconds:.2f}s")
        return "\n".join(lines)


def collect_notes(track, spec: SessionSpec) -> list[MidiNote]:
    notes: list[MidiNote] = []
    for part in track.parts:
        notes.extend(part.notes)
    return sorted(notes, key=lambda n: (n.start, n.pitch))


def render_preview(
    patch: PatchSpec,
    notes: list[MidiNote],
    bpm: float = 120.0,
    bars: int = 8,
    sample_rate: int = 44100,
    with_drums: bool = True,
    beats_per_bar: int = 4,
    seed: int = 0,
) -> np.ndarray:
    """The audition loop.

    The patch is rendered under a drum bed, because a patch judged in
    isolation is a different patch than the same patch under a beat.
    """
    result = render_track(
        patch,
        notes,
        sample_rate=sample_rate,
        bpm=bpm,
        beats_per_bar=beats_per_bar,
        seed=seed,
    )
    audio = result.audio

    if with_drums:
        drums = render_drums(sample_rate, bpm, bars, beats_per_bar)
        n = min(len(drums), len(audio))
        audio[:n, 0] += drums[:n] * 0.8
        audio[:n, 1] += drums[:n] * 0.8

    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.98:
        audio *= 0.98 / peak

    return audio


def render_session(
    spec: SessionSpec,
    out_dir: str | Path,
    sample_rate: int = 44100,
    bit_depth: int = 24,
    with_drums: bool = True,
    write_project: bool = True,
    render_dir: str | Path | None = None,
) -> SessionRender:
    """Render every track to a stem, write MIDI for melodic parts, and
    produce a REAPER project that opens with it all in place.

    `render_dir` bakes render settings into the project so it can be
    handed straight to REAPER with `-renderproject` and produce audio
    without anyone opening a dialog first.
    """
    started = time.perf_counter()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    errors = spec.validate_references()
    if errors:
        raise ValueError("invalid session:\n  " + "\n  ".join(errors))

    report = SessionRender(spec=spec)

    # Consistent length for every track so the mix lines up.
    spb = 60.0 / spec.bpm
    total_samples = int(spec.bars * spec.time_signature[0] * spb * sample_rate) + sample_rate

    stems: dict[str, np.ndarray] = {}

    for track in spec.tracks:
        render = TrackRender(track_id=track.id, name=track.name, kind=track.kind)

        if track.kind == "midi":
            patch = spec.patch(track.patch_id) if track.patch_id else None
            notes = collect_notes(track, spec)

            if patch is not None and notes:
                result = render_track(
                    patch,
                    notes,
                    sample_rate=sample_rate,
                    bpm=spec.bpm,
                    total_samples=total_samples,
                    beats_per_bar=spec.time_signature[0],
                )
                audio = result.audio
            else:
                audio = np.zeros((total_samples, 2))

            # Fit before writing, not after. A synth's release tail runs
            # past the end of the session, and a stem that overhangs the
            # session is a stem the DAW has to line up by hand.
            audio = _fit(audio, total_samples)

            stem_path = out_dir / f"{track.id}.wav"
            write_wav(stem_path, audio, sample_rate, bit_depth)
            render.stem_path = stem_path

            midi_path = out_dir / f"{track.id}.mid"
            write_midi(
                midi_path,
                notes,
                bpm=spec.bpm,
                track_name=track.name,
                program=0,
                # Carry macro positions across as CC so the intent survives
                # the handoff into a DAW that has no idea what "brightness" means.
                cc_events=[
                    (0, 74, int(round(patch.macros.brightness * 127))) for patch in [patch]
                ] if patch else [],
            )
            render.midi_path = midi_path

        else:
            if track.audio and Path(track.audio).exists():
                audio, _ = read_wav(track.audio)
                if audio.ndim == 1:
                    audio = np.column_stack([audio, audio])
                if len(audio) < total_samples:
                    audio = np.vstack([audio, np.zeros((total_samples - len(audio), audio.shape[1]))])
                audio = audio[:total_samples]
            elif track.role == "drums":
                # The context drum bed becomes a real track in the session,
                # so the artist can replace it rather than fight it.
                drums = render_drums(sample_rate, spec.bpm, spec.bars, spec.time_signature[0])
                audio = np.column_stack([drums[:total_samples], drums[:total_samples]])
            else:
                audio = np.zeros((total_samples, 2))

            audio = _fit(audio, total_samples)

            stem_path = out_dir / f"{track.id}.wav"
            write_wav(stem_path, audio, sample_rate, bit_depth)
            render.stem_path = stem_path

        # Per-track gain and pan.
        gain = _db_to_lin(track.gain_db)
        left, right = _pan_gains(track.pan)
        audio = audio * np.array([left * gain, right * gain])

        if track.muted:
            audio = audio * 0.0

        stems[track.id] = audio
        render.measurements = analyze(audio, sample_rate)
        report.tracks.append(render)

    # --- mix ---------------------------------------------------------------
    mix = np.zeros((total_samples, 2))
    for track in spec.tracks:
        mix += stems[track.id]

    # Fall back to a bare context bed only when the session has no drum track.
    if with_drums and not any(t.role == "drums" for t in spec.tracks):
        drums = render_drums(sample_rate, spec.bpm, spec.bars, spec.time_signature[0])
        n = min(len(drums), len(mix))
        mix[:n, 0] += drums[:n] * 0.8
        mix[:n, 1] += drums[:n] * 0.8

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 0.99:
        mix *= 0.99 / peak

    mix_path = out_dir / "mix.wav"
    write_wav(mix_path, mix, sample_rate, bit_depth)
    report.mix_path = mix_path

    if write_project:
        # Two views of the same session. The stems view is the mix-ready
        # handoff -- every track carries its rendered audio, so the project
        # opens with sound. The midi view carries the .mid files instead, so
        # the artist can drop a real instrument on a melodic track and
        # re-sequence it. Item lengths come from the rendered stems, because
        # REAPER uses the item length to decide how much source to play.
        durations = {t.id: total_samples / sample_rate for t in spec.tracks}
        stem = out_dir / f"{spec.title.replace(' ', '_') or 'session'}.rpp"
        write_rpp(
            stem,
            spec,
            out_dir,
            sample_rate,
            content="stems",
            durations=durations,
            render_file=(str(Path(render_dir).resolve()) + "/") if render_dir else None,
            render_pattern="agentdaw_render",
        )
        report.project_path = stem

        midi_project = out_dir / f"{spec.title.replace(' ', '_') or 'session'}-midi.rpp"
        write_rpp(midi_project, spec, out_dir, sample_rate, content="midi", durations=durations)
        report.midi_project_path = midi_project

    # --- findings ----------------------------------------------------------
    for render in report.tracks:
        report.findings.extend(report_findings(render.measurements, render.name))

    report.render_seconds = time.perf_counter() - started
    return report
