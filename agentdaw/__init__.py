"""Agent-DAW: an LLM studio producer that compiles plans into audio.

The public surface is intentionally small. Everything else is an internal
detail of how a vision becomes stems, MIDI and a REAPER project.
"""

from __future__ import annotations

__version__ = "0.1.6"

from .engine import SessionRender, TrackRender, render_preview, render_session
from .llm import LLMClient, LLMConfig, load_llm_config
from .producer import LLMProducer, Producer, StubProducer
from .spec import (
    ArrangementSection,
    Brief,
    LfoSpec,
    Macros,
    MidiNote,
    MidiPart,
    PatchSpec,
    SessionSpec,
    ShapeSpec,
    SpectrumSpec,
    TrackSpec,
)

__all__ = [
    "__version__",
    "ArrangementSection",
    "Brief",
    "LfoSpec",
    "LLMClient",
    "LLMConfig",
    "LLMProducer",
    "Macros",
    "MidiNote",
    "MidiPart",
    "PatchSpec",
    "Producer",
    "SessionRender",
    "SessionSpec",
    "ShapeSpec",
    "SpectrumSpec",
    "StubProducer",
    "TrackRender",
    "TrackSpec",
    "load_llm_config",
    "render_preview",
    "render_session",
]
