"""Generative audio providers.

The Producer does not synthesize everything. Some elements are better
generated -- textures, beds, things a subtractive engine cannot make. The
provider interface keeps that pluggable so the same session spec can target
a hosted model today and a local model tomorrow.

Every provider returns a plain audio file on disk. Nothing about the
pipeline depends on where it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class GenerationRequest:
    prompt: str
    duration_s: float = 30.0
    seed: int | None = None
    isolate: bool = False
    output_format: str = "wav"
    extra: dict = field(default_factory=dict)


@dataclass
class GenerationResult:
    provider: str
    model: str
    prompt: str
    seed: int | None
    audio_path: Path | None
    duration_s: float
    raw: dict = field(default_factory=dict)


class ProviderError(RuntimeError):
    pass


class AudioProvider(Protocol):
    name: str

    def generate(self, request: GenerationRequest, out_dir: Path) -> GenerationResult: ...
