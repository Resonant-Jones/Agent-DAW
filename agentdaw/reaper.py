"""REAPER project (.rpp) writer.

REAPER is the handoff target because its project format is plain text and
its project state is scriptable via ReaScript/Lua. That means the Producer
can write *real* project state -- tracks, items, plugin chains, tempo --
rather than dumping files into a folder and hoping.

The structure below was checked against a project that REAPER 7.62 itself
wrote on macOS (via the Rea-Cli reference corpus), not against a guess.
Three of the details are load-bearing and were wrong in the first version
of this writer:

* ``RENDER_RANGE``'s first field is the render *bounds mode*. ``0`` means
  "custom time range", and with start/end both ``0`` that renders a
  zero-length file. ``1`` means "entire project". The first version wrote
  ``0``, so the project would have rendered nothing.
* Track volume in a ``.rpp`` is a **linear** multiplier, not decibels.
  REAPER writes ``0.501187`` for -6 dB. Writing ``-6.0`` there means a
  linear gain of -6.0, i.e. -15.6 dB, and a track pulled to silence.
* A project needs its master track. REAPER expresses it as ``MASTER*``
  keys at the project level, not as a ``<TRACK`` block. Without them
  there is no master bus to render.

Two flavours are written, because they answer different questions:

``stems``
    Every track carries its rendered stem as an audio item. The project
    opens with sound, ready to mix, resample, or re-render. This is the
    mix-ready handoff.

``midi``
    Melodic tracks carry their ``.mid`` instead, so the artist can drop a
    real instrument on them and re-sequence. Audio tracks keep their WAV.
    This is the sequencing view.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .spec import SessionSpec, TrackSpec

# REAPER records the version that wrote the project. Claiming the oldest
# 7.x keeps REAPER from warning about a project from a "newer" version.
RPP_VERSION = "7.0/macOS"

# The timestamp REAPER puts in the header is seconds since the epoch; the
# value is cosmetic but REAPER writes a real one.
_EPOCH = 1700000000


def _guid(seed: str) -> str:
    """A REAPER GUID: 8-4-4-4-12 uppercase hex, like a Windows GUID."""
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest().upper()
    return "{%s-%s-%s-%s-%s}" % (
        digest[0:8],
        digest[8:12],
        digest[12:16],
        digest[16:20],
        digest[20:32],
    )


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _db_to_linear(db: float) -> float:
    """REAPER stores track volume as a linear multiplier."""
    return 10.0 ** (db / 20.0)


def _session_seconds(session: SessionSpec) -> float:
    beats = session.bars * session.time_signature[0]
    return beats * (60.0 / session.bpm)


def _note_extent_seconds(track: TrackSpec, bpm: float) -> float:
    """Seconds covered by the last note of a MIDI track."""
    end_beats = 0.0
    for part in track.parts:
        for note in part.notes:
            end_beats = max(end_beats, note.start + note.duration)
    return end_beats * (60.0 / bpm)


def _item_block(
    track: TrackSpec,
    index: int,
    source_kind: str,
    source_path: Path,
    length_seconds: float,
) -> list[str]:
    """One <ITEM> block.

    ``length_seconds`` matters: REAPER uses the item length to decide how
    much of the source to play, so a zero-length item plays nothing.
    """
    lines: list[str] = []
    lines.append("    <ITEM")
    lines.append("      POSITION 0")
    lines.append("      SNAPOFFS 0")
    lines.append(f"      LENGTH {length_seconds:.14f}")
    lines.append("      LOOP 1")
    lines.append("      ALLTAKES 0")
    lines.append("      FADEIN 1 0 0 1 0 0 0")
    lines.append("      FADEOUT 1 0 0 1 0 0 0")
    lines.append("      MUTE 0 0")
    lines.append("      SEL 0")
    lines.append(f"      IGUID {_guid(f'item:{track.id}')}")
    lines.append(f"      IID {index + 1}")
    lines.append(f'      NAME "{_escape(source_path.stem)}"')
    lines.append("      VOLPAN 1 0 1 -1")
    lines.append("      SOFFS 0")
    lines.append("      PLAYRATE 1 1 0 -1 0 0.0025")
    lines.append("      CHANMODE 0")
    lines.append(f"      GUID {_guid(f'item-guid:{track.id}')}")
    lines.append(f"      <SOURCE {source_kind}")
    lines.append(f'        FILE "{_escape(str(source_path))}"')
    lines.append("      >")
    lines.append("    >")
    return lines


def _track_block(
    track: TrackSpec,
    index: int,
    base_dir: Path,
    session: SessionSpec,
    content: str,
    durations: dict[str, float] | None,
) -> list[str]:
    lines: list[str] = []
    track_guid = _guid(f"track:{track.id}")
    lines.append(f"  <TRACK {track_guid}")
    lines.append(f'    NAME "{_escape(track.name)}"')
    lines.append("    PEAKCOL 16576")
    lines.append("    BEAT -1")
    lines.append("    AUTOMODE 0")
    lines.append("    PANLAWFLAGS 3")
    # Volume is linear in a .rpp; pan is -1..1 with 0 at centre.
    lines.append(f"    VOLPAN {_db_to_linear(track.gain_db):.6f} {track.pan:.6f} -1 -1 1")
    lines.append(f"    MUTESOLO {1 if track.muted else 0} 0 0")
    lines.append("    IPHASE 0")
    lines.append("    PLAYOFFS 0 1")
    lines.append("    ISBUS 0 0")
    lines.append("    BUSCOMP 0 0 0 0 0")
    lines.append("    SHOWINMIX 1 0.6667 0.5 1 0.5 0 0 0 0")
    lines.append("    FIXEDLANES 9 0 0 0 0")
    lines.append("    LANEREC -1 -1 -1 0")
    lines.append("    SEL 0")
    lines.append("    REC 0 0 1 0 0 0 0 0")
    lines.append("    VU 64")
    lines.append("    TRACKHEIGHT 0 0 0 0 0 0 0")
    lines.append("    INQ 0 0 0 0.5 100 0 0 100")
    lines.append("    NCHAN 2")
    lines.append("    FX 1")
    lines.append(f"    TRACKID {track_guid}")
    lines.append("    PERF 0")
    lines.append("    MIDIOUT -1")
    lines.append("    MAINSEND 1 0")

    use_midi = content == "midi" and track.kind == "midi"
    if use_midi:
        source_kind = "MIDI"
        source_path = base_dir / f"{track.id}.mid"
        length = _note_extent_seconds(track, session.bpm)
    else:
        source_kind = "WAVE"
        source_path = base_dir / f"{track.id}.wav"
        if durations and track.id in durations:
            length = durations[track.id]
        else:
            length = _session_seconds(session)

    lines.extend(_item_block(track, index, source_kind, source_path, length))
    lines.append("  >")
    return lines


def write_rpp(
    path: str | Path,
    session: SessionSpec,
    base_dir: str | Path,
    sample_rate: int = 44100,
    render_file: str | None = None,
    render_pattern: str = "render",
    render_channels: int = 2,
    content: str = "stems",
    durations: dict[str, float] | None = None,
) -> Path:
    """Write a REAPER project that opens with the produced tracks in place.

    Set `render_file` to give the project render settings. That makes it
    renderable headlessly with:

        REAPER -nosplash -profile <name> -renderproject <project.rpp>

    `content` selects what the tracks carry:

    ``stems``
        Every track references its rendered WAV. Opens with sound.
    ``midi``
        Melodic tracks reference their ``.mid`` so an instrument can be
        dropped on them; audio tracks keep their WAV.
    """
    if content not in ("stems", "midi"):
        raise ValueError(f"unknown rpp content mode: {content!r}")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = Path(base_dir)

    lines: list[str] = []
    lines.append(f'<REAPER_PROJECT 0.1 "{RPP_VERSION}" {_EPOCH} 0')
    lines.append("  RIPPLE 0 0")
    lines.append("  GROUPOVERRIDE 0 0 0 0")
    lines.append("  AUTOXFADE 129")
    lines.append("  ENVATTACH 3")
    lines.append("  POOLEDENVATTACH 0")
    lines.append("  TCPUIFLAGS 0")
    lines.append("  MIXERUIFLAGS 11 48")
    lines.append("  PEAKGAIN 1")
    lines.append("  FEEDBACK 0")
    lines.append("  PANLAW 1")
    lines.append("  PANMODE 3")
    lines.append("  PANLAWFLAGS 3")
    lines.append("  PROJOFFS 0 0 0")
    lines.append("  MAXPROJLEN 0 0")
    lines.append("  GRID 3199 8 1 8 1 0 0 0")
    lines.append("  TIMEMODE 1 5 -1 30 0 0 -1 0")
    lines.append("  CURSOR 0")
    lines.append("  ZOOM 100 0 0")
    lines.append("  VZOOMEX 6 0")
    lines.append("  USE_REC_CFG 0")
    lines.append("  RECMODE 1")
    lines.append("  SMPTESYNC 0 30 100 40 1000 300 0 0 1 0 0")
    lines.append("  RECORD_PATH \"Media\" \"\"")
    lines.append("  LOOP 0")
    lines.append("  LOOPGRAN 0 4")
    lines.append(f"  RENDER_FILE \"{render_file or ''}\"")
    lines.append(f"  RENDER_PATTERN \"{render_pattern}\"")
    lines.append(f"  RENDER_FMT 0 {render_channels} 0")
    lines.append("  RENDER_1X 0")
    # Bounds mode 1 == "entire project". Mode 0 would render a custom
    # 0..0 range, i.e. nothing.
    lines.append("  RENDER_RANGE 1 0 0 0 1000")
    lines.append("  RENDER_RESAMPLE 3 0 1")
    lines.append("  RENDER_ADDTOPROJ 0")
    lines.append("  RENDER_STEMS 0")
    lines.append("  RENDER_DITHER 0")
    lines.append("  RENDER_TRIM 0.000001 0.000001 0 0")
    lines.append("  TIMELOCKMODE 1")
    lines.append("  TEMPOENVLOCKMODE 1")
    lines.append("  ITEMMIX 1")
    lines.append("  DEFPITCHMODE 589824 0")
    lines.append("  TAKELANE 1")
    lines.append(f"  SAMPLERATE {sample_rate} 0 0")
    lines.append("  LOCK 1")
    lines.append("  GLOBAL_AUTO -1")
    lines.append("  TEMPO %.6f %d %d" % (session.bpm, session.time_signature[0], session.time_signature[1]))
    lines.append("  PLAYRATE 1 0 0.25 4")
    lines.append("  SELECTION 0 0")
    lines.append("  SELECTION2 0 0")
    # -- master track. Without these there is no master bus to render. --
    lines.append("  MASTERAUTOMODE 0")
    lines.append("  MASTERTRACKHEIGHT 0 0")
    lines.append("  MASTERPEAKCOL 16576")
    lines.append("  MASTERMUTESOLO 0")
    lines.append("  MASTERTRACKVIEW 0 0.6667 0.5 0.5 0 0 0 0 0 0 0 0 0 0 0")
    lines.append("  MASTERHWOUT 0 0 1 0 0 0 0 -1")
    lines.append("  MASTER_NCH 2 2")
    lines.append("  MASTER_VOLUME 1 0 -1 -1 1")
    lines.append("  MASTER_PANMODE 3")
    lines.append("  MASTER_PANLAWFLAGS 3")
    lines.append("  MASTER_FX 1")
    lines.append("  MASTER_SEL 0")
    lines.append("  RULERHEIGHT 84 84")

    for i, track in enumerate(session.tracks):
        lines.extend(_track_block(track, i, base_dir, session, content, durations))

    lines.append(">")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
