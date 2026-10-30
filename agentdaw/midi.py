"""Standard MIDI File writer.

MIDI is the one interchange format every DAW understands without
negotiation, which makes it the natural handoff for anything the Producer
composes. No third-party library: the SMF format is small and writing it
directly keeps the dependency list honest.
"""

from __future__ import annotations

from pathlib import Path

from .spec import MidiNote


def _vlq(value: int) -> bytes:
    """Variable-length quantity."""
    value = max(0, int(value))
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + len(payload).to_bytes(4, "big") + payload


def _meta(event_type: int, payload: bytes) -> bytes:
    return bytes([0xFF, event_type]) + _vlq(len(payload)) + payload


class MidiTrack:
    """Accumulates events with absolute tick positions."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.events: list[tuple[int, bytes]] = []

    def add(self, tick: int, data: bytes) -> None:
        self.events.append((int(tick), data))

    def to_bytes(self) -> bytes:
        ordered = sorted(self.events, key=lambda e: e[0])
        payload = bytearray()
        if self.name:
            # Every event in a track is preceded by a delta time, meta
            # events included. Omitting it makes the track name's 0xFF
            # status byte get read as a delta, which swallows the track.
            payload += _vlq(0) + _meta(0x03, self.name.encode("utf-8")[:127])
        prev = 0
        for tick, data in ordered:
            payload += _vlq(tick - prev)
            payload += data
            prev = tick
        payload += _vlq(0) + b"\xFF\x2F\x00"  # end of track
        return _chunk(b"MTrk", bytes(payload))


def write_midi(
    path: str | Path,
    notes: list[MidiNote],
    bpm: float = 120.0,
    ticks_per_beat: int = 480,
    track_name: str = "Producer",
    program: int = 0,
    channel: int = 0,
    cc_events: list[tuple[int, int, int]] | None = None,
) -> Path:
    """Write a format-1 MIDI file containing one part.

    `cc_events` are (tick, controller, value) triples -- used to carry
    filter and macro positions across to a DAW so the handoff preserves the
    Producer's intent rather than just the notes.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Track 0: tempo and time signature.
    meta = MidiTrack("Tempo")
    micros = int(round(60_000_000 / bpm))
    meta.add(0, _meta(0x51, micros.to_bytes(3, "big")))
    meta.add(0, _meta(0x58, bytes([4, 2, 24, 8])))  # 4/4, 24 clocks per metronome click
    meta.add(0, _meta(0x59, bytes([2, 3])))  # key signature: 2 sharps, minor
    meta.add(0, _meta(0x03, track_name.encode("utf-8")[:127]))

    part = MidiTrack(track_name)
    part.add(0, bytes([0xC0 | (channel & 0x0F), program & 0x7F]))

    for tick, controller, value in (cc_events or []):
        part.add(int(tick), bytes([0xB0 | (channel & 0x0F), controller & 0x7F, max(0, min(127, int(value)))]))

    for note in notes:
        start = int(round(note.start * ticks_per_beat))
        end = start + max(1, int(round(note.duration * ticks_per_beat)))
        vel = max(1, min(127, int(round(note.velocity * 127))))
        part.add(start, bytes([0x90 | (channel & 0x0F), note.pitch & 0x7F, vel]))
        part.add(end, bytes([0x80 | (channel & 0x0F), note.pitch & 0x7F, 0]))

    header = _chunk(b"MThd", (1).to_bytes(2, "big") + (2).to_bytes(2, "big") + ticks_per_beat.to_bytes(2, "big"))
    payload = header + meta.to_bytes() + part.to_bytes()

    path.write_bytes(payload)
    return path


def read_midi(path: str | Path) -> list[MidiNote]:
    """Minimal reader used by tests to confirm the writer round-trips."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"MThd":
        raise ValueError("not a MIDI file")

    pos = 0
    notes: list[MidiNote] = []
    pending: dict[tuple[int, int], tuple[int, int]] = {}

    while pos < len(raw):
        tag = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "big")
        body = raw[pos + 8:pos + 8 + size]
        pos += 8 + size

        if tag != b"MTrk":
            continue

        i = 0
        tick = 0
        running_status = None
        while i < len(body):
            # variable-length delta
            delta = 0
            while i < len(body):
                byte = body[i]
                i += 1
                delta = (delta << 7) | (byte & 0x7F)
                if not byte & 0x80:
                    break
            tick += delta

            status = body[i]
            if status & 0x80:
                running_status = status
                i += 1
            else:
                status = running_status
                if status is None:
                    break

            kind = status & 0xF0
            channel = status & 0x0F

            if kind == 0x90:
                pitch, vel = body[i], body[i + 1]
                i += 2
                if vel > 0:
                    pending[(channel, pitch)] = (tick, vel)
                else:
                    start = pending.pop((channel, pitch), None)
                    if start:
                        notes.append(MidiNote(
                            pitch=pitch,
                            start=start[0] / 480.0,
                            duration=max(1, tick - start[0]) / 480.0,
                            velocity=start[1] / 127.0,
                        ))
            elif kind == 0x80:
                # Explicit note-off, not the note-on-with-velocity-zero form.
                pitch = body[i]
                i += 2
                start = pending.pop((channel, pitch), None)
                if start:
                    notes.append(MidiNote(
                        pitch=pitch,
                        start=start[0] / 480.0,
                        duration=max(1, tick - start[0]) / 480.0,
                        velocity=start[1] / 127.0,
                    ))
            elif kind in (0xA0, 0xB0, 0xE0):
                i += 2
            elif kind in (0xC0, 0xD0):
                i += 1
            elif status == 0xFF:
                meta_type = body[i]
                i += 1
                length = 0
                while i < len(body):
                    byte = body[i]
                    i += 1
                    length = (length << 7) | (byte & 0x7F)
                    if not byte & 0x80:
                        break
                i += length
                # A meta event cancels running status.
                running_status = None
            else:
                break

    return notes
