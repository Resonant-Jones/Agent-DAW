"""The approved-patch library: retrieval anchors and delta revisions.

The Producer only compounds if it can recognise a sound it has already
made. The library is that memory. Every patch the artist approves becomes
an *anchor* -- named, measured, retrievable -- so a later request revises
a known patch by delta instead of inventing something new.

Three rules shape this module, in priority order.

**A revision is a complete new state, never a patch.** A library entry
holds the entire macro vector and the entire spectrum. A revision
produces a new entry with a complete new vector, records which entry it
came from, and records what changed in the artist's words. Nothing is
ever half-updated: there is no state in which one value is retained while
its partner is re-derived.

**Retrieval is measured, not vibes.** Every entry carries the numbers the
analysis pass produced -- loudness, spectral centroid, band balance,
stereo width -- alongside the artist's own words. Retrieval ranks on both,
so "a brighter version of that pad" finds the pad and can say which way
to move and by how much.

**A miss is a legitimate outcome.** When nothing in the library is close,
retrieval says so rather than pretending. The Producer is then free to
invent, and the new entry records that it was born new rather than
derived.

The store is a plain JSON file. It is deliberately storage-agnostic: the
same entries can live in Codexify's long-term memory, in a git-tracked
file, or in a database, without this module caring.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from .spec import PatchSpec

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

# The artist's words, mapped onto the six semantic axes. This is the whole
# point of the semantic spec: the artist never says "raise brightness by
# 0.1", they say "brighter", and the library knows which axis that is.
DIRECTION_LEXICON: dict[str, tuple[str, float]] = {
    "brighter": ("brightness", +1.0),
    "bright": ("brightness", +1.0),
    "sparkly": ("brightness", +1.0),
    "crisp": ("brightness", +1.0),
    "present": ("brightness", +1.0),
    "sharper": ("brightness", +1.0),
    "darker": ("brightness", -1.0),
    "dark": ("brightness", -1.0),
    "muffled": ("brightness", -1.0),
    "veiled": ("brightness", -1.0),
    "warmer": ("body", +1.0),
    "warm": ("body", +1.0),
    "rounder": ("body", +1.0),
    "fuller": ("body", +1.0),
    "colder": ("body", -1.0),
    "cold": ("body", -1.0),
    "hollow": ("body", -1.0),
    "wider": ("size", +1.0),
    "wide": ("size", +1.0),
    "airy": ("size", +1.0),
    "bigger": ("size", +1.0),
    "thicker": ("size", +1.0),
    "narrower": ("size", -1.0),
    "narrow": ("size", -1.0),
    "focused": ("size", -1.0),
    "grittier": ("grit", +1.0),
    "gritty": ("grit", +1.0),
    "dirtier": ("grit", +1.0),
    "rawer": ("grit", +1.0),
    "cleaner": ("grit", -1.0),
    "clean": ("grit", -1.0),
    "smoother": ("grit", -1.0),
    "alive": ("motion", +1.0),
    "animated": ("motion", +1.0),
    "moving": ("motion", +1.0),
    "still": ("motion", -1.0),
    "static": ("motion", -1.0),
    "spacious": ("space", +1.0),
    "reverberant": ("space", +1.0),
    "huge": ("space", +1.0),
    "drier": ("space", -1.0),
    "dry": ("space", -1.0),
    "tight": ("space", -1.0),
}

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "that", "this", "it", "is", "are", "be", "as", "at", "by", "from",
    "make", "give", "more", "less", "like", "some", "very", "much", "but",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with stopwords removed."""
    return [
        t for t in _TOKEN_RE.findall(text.lower())
        if t not in _STOPWORDS and len(t) > 2
    ]


def canonical_tokens(text: str) -> set[str]:
    """Tokens plus their canonical axis words, so synonyms match."""
    tokens = set(tokenize(text))
    for token in list(tokens):
        mapped = DIRECTION_LEXICON.get(token)
        if mapped:
            tokens.add(mapped[0])
    return tokens


def axis_directions(text: str) -> dict[str, float]:
    """Which axes the artist asked to move, and in which direction.

    "brighter and wider" -> {"brightness": 1.0, "size": 1.0}
    """
    directions: dict[str, float] = {}
    for token in tokenize(text):
        mapped = DIRECTION_LEXICON.get(token)
        if not mapped:
            continue
        axis, sign = mapped
        directions[axis] = directions.get(axis, 0.0) + sign
    return directions


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where an entry came from and what the artist asked for."""

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = Field(default="", description="Session id, file path, or 'manual'.")
    brief: str = Field(default="", description="The artist's words at the time.")
    origin: Literal["new", "revision"] = "new"
    parent_id: str | None = Field(
        default=None, description="The entry this one was revised from."
    )
    delta: str | None = Field(
        default=None, description="What the artist asked to change."
    )


class LibraryEntry(BaseModel):
    """One approved patch, with everything needed to find it again."""

    id: str
    name: str
    description: str = ""
    patch: PatchSpec
    anchors: list[str] = Field(
        default_factory=list,
        description="The artist's vocabulary for this sound.",
    )
    measurements: dict[str, float] = Field(
        default_factory=dict,
        description="Measured facts from the analysis pass. Not opinions.",
    )
    provenance: Provenance = Field(default_factory=Provenance)

    @property
    def macros(self) -> dict[str, float]:
        return self.patch.macros.model_dump()

    def summary(self) -> dict[str, Any]:
        """The compact form the Producer sees in its prompt."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "macros": self.macros,
            "spectrum": self.patch.spectrum.model_dump(),
            "shape": self.patch.shape.model_dump(),
            "lfo": self.patch.lfo.model_dump(),
            "seed": self.patch.seed,
            "anchors": self.anchors,
            "measurements": self.measurements,
            "origin": self.provenance.origin,
            "parent_id": self.provenance.parent_id,
            "delta": self.provenance.delta,
        }


class RetrievalHit(BaseModel):
    """A library entry found for a request, with the reasoning attached."""

    entry: LibraryEntry
    score: float = Field(..., ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    direction: dict[str, float] = Field(
        default_factory=dict,
        description="Which axes the request asked to move, and how far.",
    )
    suggested_macros: dict[str, float] = Field(
        default_factory=dict,
        description="The anchor's macros with the requested movement applied.",
    )

    def summary(self) -> dict[str, Any]:
        out = self.entry.summary()
        out["retrieval_score"] = round(self.score, 4)
        out["why"] = self.reasons
        if self.direction:
            out["requested_direction"] = self.direction
        if self.suggested_macros:
            out["suggested_macros"] = self.suggested_macros
        return out


# --------------------------------------------------------------------------
# The library
# --------------------------------------------------------------------------


def _macro_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """Mean absolute distance over the axes both sides define, 0..1."""
    keys = set(a) & set(b)
    if not keys:
        return 1.0
    return sum(abs(a[k] - b[k]) for k in keys) / len(keys)


def _measurement_proximity(
    target: dict[str, float], entry: dict[str, float]
) -> float:
    """0..1 closeness on the measured facts, 1.0 = identical.

    With no target measurements there is nothing to compare against, so
    the answer is neutral (0.5) rather than a fabricated similarity.
    Centroid is compared in log space because brightness is a ratio, not a
    distance: 400 Hz to 800 Hz is the same move as 4 kHz to 8 kHz.
    """
    if not target:
        return 0.5

    scores: list[float] = []

    centroid_a = target.get("spectral_centroid_hz")
    centroid_b = entry.get("spectral_centroid_hz")
    if centroid_a and centroid_b and centroid_a > 0 and centroid_b > 0:
        ratio = abs(math.log2(centroid_a / centroid_b))
        scores.append(max(0.0, 1.0 - ratio / 2.0))

    lufs_a = a.get("integrated_lufs")
    lufs_b = a.get("integrated_lufs")
    if lufs_a is not None and lufs_b is not None:
        scores.append(max(0.0, 1.0 - abs(lufs_a - lufs_b) / 12.0))

    width_a = a.get("stereo_width")
    width_b = a.get("stereo_width")
    if width_a is not None and width_b is not None:
        scores.append(max(0.0, 1.0 - abs(width_a - width_b)))

    if not scores:
        return 0.5
    return sum(scores) / len(scores)


# Which measured fact speaks to which semantic axis. This is how a request
# that says "brighter" gets anchored to a number rather than a vibe.
AXIS_MEASUREMENT: dict[str, tuple[str, str, str]] = {
    "brightness": ("spectral_centroid_hz", "Hz", "spectral centroid"),
    "body": ("low_energy_db", "dB", "low-band energy"),
    "size": ("stereo_width", "", "stereo width"),
    "space": ("loudness_range_lu", "LU", "loudness range"),
    "grit": ("spectral_flatness", "", "spectral flatness"),
    "motion": ("crest_factor_db", "dB", "crest factor"),
}


class PatchLibrary:
    """A collection of approved patches, stored as JSON."""

    VERSION = "1.0"

    def __init__(self, entries: Iterable[LibraryEntry] | None = None) -> None:
        self.entries: list[LibraryEntry] = list(entries or [])

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "PatchLibrary":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return cls([LibraryEntry.model_validate(e) for e in raw])
        entries = raw.get("entries", [])
        return cls([LibraryEntry.model_validate(e) for e in entries])

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "entries": [e.model_dump() for e in self.entries],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def get(self, entry_id: str) -> LibraryEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    # -- authoring ---------------------------------------------------------

    def add(self, entry: LibraryEntry) -> LibraryEntry:
        """Add an entry. Ids are unique; re-adding an id replaces it."""
        existing = self.get(entry.id)
        if existing is not None:
            self.entries[self.entries.index(existing)] = entry
        else:
            self.entries.append(entry)
        return entry

    def revise(
        self,
        parent_id: str,
        patch: PatchSpec,
        feedback: str,
        name: str | None = None,
        anchors: list[str] | None = None,
    ) -> LibraryEntry:
        """Record a revision as a new entry with a complete new state.

        The parent is never mutated. The new entry carries the whole
        macro vector and the whole spectrum, plus its parent and the
        artist's words describing the change.
        """
        parent = self.get(parent_id)
        if parent is None:
            raise KeyError(f"no such library entry: {parent_id}")

        new_id = f"{parent.id}_r{self._revision_count(parent.id) + 1}"
        entry = LibraryEntry(
            id=new_id,
            name=name or f"{parent.name} (rev)",
            description=parent.description,
            patch=patch,
            anchors=list(anchors) if anchors is not None else list(parent.anchors),
            measurements=dict(parent.measurements),
            provenance=Provenance(
                source=parent.provenance.source,
                brief=parent.provenance.brief,
                origin="revision",
                parent_id=parent.id,
                delta=feedback,
            ),
        )
        self.entries.append(entry)
        return entry

    def _revision_count(self, parent_id: str) -> int:
        return sum(1 for e in self.entries if e.provenance.parent_id == parent_id)

    # -- retrieval ---------------------------------------------------------

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.15,
        target_measurements: dict[str, float] | None = None,
    ) -> list[RetrievalHit]:
        """Find anchors for a request.

        Ranks on the artist's words, the macro vector and the measured
        facts, and reports why each hit matched. A query that shares
        nothing with the library returns nothing, which is a legitimate
        answer: the Producer then invents, and the result is recorded as
        new rather than derived.
        """
        query_tokens = canonical_tokens(query)
        directions = axis_directions(query)
        hits: list[RetrievalHit] = []

        for entry in self.entries:
            entry_tokens = canonical_tokens(
                " ".join([entry.name, entry.description, *entry.anchors])
            )
            union = query_tokens | entry_tokens
            overlap = len(query_tokens & entry_tokens) / len(union) if union else 0.0

            macro_sim = 1.0 - _macro_distance(query_macros(query), entry.macros)
            measured = _measurement_proximity(target_measurements or {}, entry.measurements)

            score = 0.45 * overlap + 0.35 * max(0.0, macro_sim) + 0.20 * measured
            if score < min_score:
                continue

            reasons: list[str] = []
            shared = sorted(query_tokens & entry_tokens)
            if shared:
                reasons.append("shares words with the request: " + ", ".join(shared))
            if macro_sim > 0.75:
                reasons.append(
                    f"macro position is close "
                    f"({1 - macro_sim:.2f} away across the six axes)"
                )
            if measured > 0.8:
                reasons.append("measured profile is close to the anchor's")

            suggested: dict[str, float] = {}
            for axis, sign in directions.items():
                current = entry.macros.get(axis)
                if current is None:
                    continue
                # A step of 0.25 on the semantic axis: enough to hear,
                # small enough to stay recognisably the same patch.
                suggested[axis] = round(min(1.0, max(0.0, current + sign * 0.25)), 4)

            if suggested:
                moved = ", ".join(
                    f"{axis} {entry.macros.get(axis, 0.0):.2f} -> {value:.2f}"
                    for axis, value in suggested.items()
                )
                reasons.append(f"request asks to move {moved}")

                # Quote the measurement that backs the move, so the request
                # is anchored to a number and not to a vibe.
                for axis in suggested:
                    fact = AXIS_MEASUREMENT.get(axis)
                    if not fact:
                        continue
                    key, unit, label = fact
                    value = entry.measurements.get(key)
                    if value is None:
                        continue
                    shown = f"{value:.1f}{unit}" if unit else f"{value:.3f}"
                    reasons.append(
                        f"measured {label} of this anchor is {shown}"
                    )

            hits.append(
                RetrievalHit(
                    entry=entry,
                    score=round(score, 4),
                    reasons=reasons,
                    direction=directions,
                    suggested_macros=suggested,
                )
            )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    # -- reporting ---------------------------------------------------------

    def lineage(self, entry_id: str) -> list[LibraryEntry]:
        """The chain of revisions that produced an entry, oldest first."""
        chain: list[LibraryEntry] = []
        current = self.get(entry_id)
        while current is not None:
            chain.append(current)
            parent_id = current.provenance.parent_id
            current = self.get(parent_id) if parent_id else None
        return list(reversed(chain))


def query_macros(query: str) -> dict[str, float]:
    """Extract a macro position implied by a request's own words.

    A request that says "bright" but nothing else does not mean brightness
    1.0 -- it means the other axes are unknown. So this returns only the
    axes the request actually named, and the retrieval distance falls back
    to the axes both sides share.
    """
    macros: dict[str, float] = {}
    for axis, sign in axis_directions(query).items():
        macros[axis] = 0.75 if sign > 0 else 0.25
    return macros


def describe_patch(
    patch: PatchSpec,
    name: str = "",
    description: str = "",
) -> list[str]:
    """Derive anchors for a patch from what it actually is.

    The anchors are read off the macro vector and the description, so they
    describe the sound rather than repeating its name.
    """
    anchors: list[str] = []
    macros = patch.macros

    if macros.brightness >= 0.65:
        anchors.append("bright")
    elif macros.brightness <= 0.35:
        anchors.append("dark")
    if macros.body >= 0.65:
        anchors.append("warm")
    elif macros.body <= 0.35:
        anchors.append("hollow")
    if macros.size >= 0.65:
        anchors.append("wide")
    elif macros.size <= 0.35:
        anchors.append("narrow")
    if macros.grit >= 0.5:
        anchors.append("gritty")
    if macros.space >= 0.65:
        anchors.append("spacious")
    elif macros.space <= 0.25:
        anchors.append("dry")
    if macros.motion >= 0.65:
        anchors.append("alive")

    for word in tokenize(description):
        if word not in anchors and len(anchors) < 12:
            anchors.append(word)

    if name:
        anchors.insert(0, name.lower())
    return list(dict.fromkeys(anchors))
