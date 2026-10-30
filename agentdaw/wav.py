"""WAV writing. No third-party audio library required.

Supports 16/24/32-bit PCM and 32-bit float, because stems that leave this
system need to be readable by whatever DAW the artist actually owns.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def _to_pcm(data: np.ndarray, bit_depth: int) -> bytes:
    if data.ndim == 1:
        data = data[:, None]
    n_channels = data.shape[1]

    if bit_depth == 16:
        scaled = np.clip(data, -1.0, 1.0) * 32767.0
        ints = scaled.astype(np.int16)
        return ints.tobytes()

    if bit_depth == 24:
        scaled = np.clip(data, -1.0, 1.0) * 8388607.0
        ints = scaled.astype(np.int32)
        out = bytearray()
        for v in ints.ravel():
            v = int(v)
            out += bytes(((v >> 0) & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
        return bytes(out)

    if bit_depth == 32:
        scaled = np.clip(data, -1.0, 1.0) * 2147483647.0
        ints = scaled.astype(np.int32)
        return ints.tobytes()

    raise ValueError(f"unsupported bit depth: {bit_depth}")


def write_wav(
    path: str | Path,
    data: np.ndarray,
    sample_rate: int = 44100,
    bit_depth: int = 24,
    float_format: bool = False,
) -> Path:
    """Write a WAV file.

    `data` is (n_samples,) mono or (n_samples, n_channels).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if data.ndim == 1:
        data = data[:, None]
    data = np.ascontiguousarray(data, dtype=np.float64)

    n_channels = data.shape[1]
    n_frames = data.shape[0]

    if float_format:
        audio_format = 3  # IEEE float
        bits = 32
        fmt_chunk_size = 18
        payload = np.clip(data, -1.0, 1.0).astype("<f4").tobytes()
    else:
        audio_format = 1  # PCM
        bits = bit_depth
        fmt_chunk_size = 16
        payload = _to_pcm(data, bit_depth)

    byte_rate = sample_rate * n_channels * bits // 8
    block_align = n_channels * bits // 8

    fmt = struct.pack(
        "<HHIIHH",
        audio_format,
        n_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
    )
    if float_format:
        fmt += struct.pack("<H", 0)  # cbSize

    data_chunk = struct.pack("<I", len(payload)) + payload
    fmt_chunk = struct.pack("<I", fmt_chunk_size) + fmt

    riff_size = 4 + len(fmt_chunk) + len(data_chunk)
    with open(path, "wb") as fh:
        fh.write(b"RIFF")
        fh.write(struct.pack("<I", riff_size))
        fh.write(b"WAVE")
        fh.write(b"fmt ")
        fh.write(fmt_chunk)
        fh.write(b"data")
        fh.write(data_chunk)

    return path


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a PCM or float WAV. Used by the analysis pass and by tests."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"not a RIFF/WAVE file: {path}")

    pos = 12
    fmt: tuple | None = None
    payload: bytes | None = None

    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            payload = body
        pos += 8 + size + (size & 1)

    if fmt is None or payload is None:
        raise ValueError(f"missing fmt or data chunk: {path}")

    audio_format, n_channels, sample_rate, _, _, bits = fmt
    if audio_format == 3 and bits == 32:
        data = np.frombuffer(payload, dtype="<f4").astype(np.float64)
    elif bits == 16:
        data = np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0
    elif bits == 24:
        raw_bytes = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
        ints = (
            raw_bytes[:, 0].astype(np.int32)
            | (raw_bytes[:, 1].astype(np.int32) << 8)
            | (raw_bytes[:, 2].astype(np.int32) << 16)
        )
        ints = np.where(ints & 0x800000, ints - (1 << 24), ints)
        data = ints.astype(np.float64) / 8388608.0
    elif bits == 32:
        data = np.frombuffer(payload, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported bit depth {bits} in {path}")

    if n_channels > 1:
        data = data.reshape(-1, n_channels)

    return data, sample_rate
