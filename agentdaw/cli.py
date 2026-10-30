"""Command line interface.

    agentdaw doctor                      # what is configured, what is missing
    agentdaw demo                        # render the bundled session, no LLM
    agentdaw compose "a warm analog pad" # Producer -> spec -> stems -> project
    agentdaw preview spec.json pad_warm  # fast audition of one patch
    agentdaw measure out/mix.wav         # measurements for a file
    agentdaw generate --provider stable_audio --prompt "..."
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .analyze import analyze, report_findings
from .engine import render_preview, render_session
from .llm import LLMClient, load_llm_config
from .producer import LLMProducer, StubProducer
from .spec import SessionSpec
from .wav import read_wav, write_wav

DEFAULT_OUT = Path("out")


def _print_spec_brief(spec: SessionSpec) -> None:
    b = spec.brief
    print("\n  BRIEF")
    for field in ("goal", "deliverable", "tempo", "layers", "midi_audio",
                  "length", "energy", "structure", "movement", "production", "variations"):
        value = getattr(b, field, "")
        if value:
            print(f"    {field:<12} {value}")
    if b.locked:
        print(f"    {'locked':<12} {', '.join(b.locked)}")
    print()


def cmd_library(args: argparse.Namespace) -> int:
    """Approve, list and retrieve from the patch library."""
    from .library import LibraryEntry, PatchLibrary, Provenance, describe_patch

    action = args.action

    if action == "approve":
        spec = SessionSpec.model_validate(
            json.loads(Path(args.spec).read_text(encoding="utf-8"))
        )
        measurements: dict[str, dict[str, float]] = {}
        if args.measurements and Path(args.measurements).exists():
            measurements = json.loads(Path(args.measurements).read_text(encoding="utf-8"))

        library = (
            PatchLibrary.load(args.library)
            if args.library and Path(args.library).exists()
            else PatchLibrary()
        )

        for patch in spec.patches:
            track = next(
                (t for t in spec.tracks if t.patch_id == patch.id), None
            )
            measured_raw = measurements.get(track.id if track else "", {})
            # Keep the measured numbers; drop anything that is not one
            # (the analysis pass also emits free-text notes).
            measured = {
                k: float(v)
                for k, v in measured_raw.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            entry = LibraryEntry(
                id=patch.id,
                name=patch.name,
                description=patch.description,
                patch=patch,
                anchors=describe_patch(patch, patch.name, patch.description),
                measurements=measured,
                provenance=Provenance(
                    source=f"session:{spec.title}",
                    brief=spec.brief.goal,
                    origin="new",
                ),
            )
            library.add(entry)
            print(f"  approved {entry.id:<16} anchors: {', '.join(entry.anchors[:6])}")

        out = Path(args.out_file or (args.library or "patch-library.json"))
        library.save(out)
        print(f"  library  {out}  ({len(library.entries)} entries)")
        return 0

    if action == "list":
        library = PatchLibrary.load(args.library)
        for entry in library.entries:
            origin = entry.provenance.origin
            parent = f" <- {entry.provenance.parent_id}" if entry.provenance.parent_id else ""
            print(f"  {entry.id:<20} {entry.name:<24} [{origin}]{parent}")
            if entry.provenance.delta:
                print(f"    delta: {entry.provenance.delta}")
            if entry.anchors:
                print(f"    anchors: {', '.join(entry.anchors[:8])}")
        print(f"  {len(library.entries)} entries in {args.library}")
        return 0

    if action == "find":
        library = PatchLibrary.load(args.library)
        hits = library.retrieve(args.query, limit=args.limit)
        if not hits:
            print("  no anchor matches that request -- the Producer would invent.")
            return 0
        for hit in hits:
            entry = hit.entry
            print(f"  {entry.id:<20} score {hit.score:.3f}  {entry.name}")
            for reason in hit.reasons:
                print(f"    - {reason}")
            if hit.suggested_macros:
                moves = ", ".join(
                    f"{axis} {entry.macros.get(axis, 0.0):.2f}->{value:.2f}"
                    for axis, value in hit.suggested_macros.items()
                )
                print(f"    suggested: {moves}")
        return 0

    print(f"  unknown library action: {action}")
    return 2


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_llm_config(model=args.model)
    print("  LLM")
    if config.available:
        print(f"    provider     {config.provider}")
        print(f"    protocol     {config.protocol}")
        print(f"    model        {config.model}")
        print(f"    base url     {config.base_url}")
        print(f"    api key      {'set' if config.api_key else 'MISSING'}")
        if config.auth_source and config.auth_source not in ("config.yaml", "environment"):
            print(f"    auth source  {config.auth_source}")
    else:
        print("    no LLM configured")
        print("    set AGENTDAW_LLM_BASE_URL + AGENTDAW_LLM_MODEL to override")

    print("\n  Generative audio")
    for name, env in (("stable_audio", "STABILITY_API_KEY"), ("lyria", "GEMINI_API_KEY")):
        import os
        print(f"    {name:<14} {'key set' if os.environ.get(env) else 'no key (' + env + ')'}")

    print("\n  Local tools")
    for tool in ("ffmpeg", "ffprobe"):
        import shutil
        path = shutil.which(tool)
        print(f"    {tool:<14} {path or 'not found'}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    producer = StubProducer()
    spec = producer.compose("A warm, wide pad over a slow bass groove")
    return _render(spec, out_dir, args)


def cmd_compose(args: argparse.Namespace) -> int:
    config = load_llm_config(model=args.model)
    if not config.available:
        print("  no LLM configured. Run `agentdaw doctor`, or use `agentdaw demo`.")
        return 2

    client = LLMClient(config)
    producer = LLMProducer(client)

    library = None
    if args.library and Path(args.library).exists():
        from .library import PatchLibrary

        try:
            library = PatchLibrary.load(args.library)
        except Exception:
            # A plain list of anchors is still accepted.
            library = json.loads(Path(args.library).read_text())

    started = time.perf_counter()
    try:
        spec = producer.compose(args.vision, patch_library=library)
    except Exception as exc:
        print(f"  producer failed: {exc}")
        return 1
    elapsed = time.perf_counter() - started

    print(f"  producer finished in {elapsed:.1f}s")
    _print_spec_brief(spec)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / "session.json"
    spec_path.write_text(spec.model_dump_json(indent=2))
    print(f"  spec written to {spec_path}")

    return _render(spec, out_dir, args)


def cmd_revise(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"  no such spec: {spec_path}")
        return 2

    config = load_llm_config(model=args.model)
    if not config.available:
        print("  no LLM configured.")
        return 2

    existing = SessionSpec.model_validate(json.loads(spec_path.read_text()))
    producer = LLMProducer(LLMClient(config))
    spec = producer.compose(existing.title, existing=existing, feedback=args.feedback)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "session.json").write_text(spec.model_dump_json(indent=2))
    _print_spec_brief(spec)
    return _render(spec, out_dir, args)


def cmd_preview(args: argparse.Namespace) -> int:
    spec = SessionSpec.model_validate(json.loads(Path(args.spec).read_text()))
    patch = spec.patch(args.patch)
    if patch is None:
        print(f"  no patch named {args.patch}. Available: "
              f"{', '.join(p.id for p in spec.patches)}")
        return 2

    track = next((t for t in spec.tracks if t.patch_id == args.patch), None)
    notes = []
    if track:
        for part in track.parts:
            notes.extend(part.notes)

    started = time.perf_counter()
    audio = render_preview(
        patch, notes,
        bpm=spec.bpm,
        bars=spec.bars,
        sample_rate=args.sample_rate,
        with_drums=not args.no_drums,
        beats_per_bar=spec.time_signature[0],
    )
    elapsed = time.perf_counter() - started

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"preview_{patch.id}.wav"
    write_wav(out_path, audio, args.sample_rate, 24)

    print(f"  rendered {len(audio) / args.sample_rate:.1f}s of audio in {elapsed:.2f}s")
    print(f"  {out_path}")
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    data, sr = read_wav(args.file)
    m = analyze(data, sr)
    print(f"  {args.file}")
    print(f"    duration        {m.duration_s:.2f}s @ {m.sample_rate} Hz")
    print(f"    peak            {m.peak_dbfs:.2f} dBFS")
    print(f"    rms             {m.rms_dbfs:.2f} dBFS")
    print(f"    crest           {m.crest_factor_db:.2f} dB")
    print(f"    integrated      {m.integrated_lufs:.2f} LUFS")
    print(f"    loudness range  {m.loudness_range_lu:.2f} LU")
    print(f"    centroid        {m.spectral_centroid_hz:.0f} Hz")
    print(f"    rolloff 85      {m.spectral_rolloff_85_hz:.0f} Hz")
    print(f"    bands L/M/H     {m.low_energy_db:.1f} / {m.mid_energy_db:.1f} / {m.high_energy_db:.1f} dB")
    print(f"    stereo width    {m.stereo_width:.3f}")
    findings = report_findings(m)
    if findings:
        print("\n  findings")
        for f in findings:
            print(f"    - {f}")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    from .providers import GenerationRequest

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.provider == "stable_audio":
        from .providers.stable_audio import StableAudioProvider
        provider = StableAudioProvider()
    elif args.provider == "lyria":
        from .providers.lyria import LyriaProvider
        provider = LyriaProvider(model=args.model or None)
    else:
        print(f"  unknown provider: {args.provider}")
        return 2

    request = GenerationRequest(
        prompt=args.prompt,
        duration_s=args.duration,
        seed=args.seed,
        isolate=args.isolate,
    )
    try:
        result = provider.generate(request, out_dir)
    except Exception as exc:
        print(f"  generation failed: {exc}")
        return 1

    print(f"  {result.provider} / {result.model}")
    print(f"  prompt   {result.prompt}")
    print(f"  duration {result.duration_s:.0f}s")
    print(f"  file     {result.audio_path}")
    return 0


def cmd_reaper_render(args: argparse.Namespace) -> int:
    """Render a .rpp through REAPER."""
    from .reaper_render import ReaperError, render_project

    project = Path(args.project)
    out_dir = Path(args.out)

    try:
        result = render_project(project, out_dir, timeout=args.timeout)
    except ReaperError as exc:
        print(f"  {exc}")
        return 1

    print(f"  REAPER rendered {project.name} -> {result}")
    print(f"    {result.stat().st_size} bytes")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    from .schema import json_schema_bundle

    bundle = json_schema_bundle()
    if args.name:
        if args.name not in bundle:
            print(f"  unknown schema: {args.name}. Available: {', '.join(bundle)}")
            return 2
        bundle = {args.name: bundle[args.name]}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, schema in bundle.items():
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2))
        print(f"  {path}")
    return 0


def _render(spec: SessionSpec, out_dir: Path, args: argparse.Namespace) -> int:
    errors = spec.validate_references()
    if errors:
        print("  invalid spec:")
        for e in errors:
            print(f"    - {e}")
        return 1

    report = render_session(
        spec,
        out_dir,
        sample_rate=args.sample_rate,
        bit_depth=args.bit_depth,
        with_drums=not args.no_drums,
        render_dir=getattr(args, "render_dir", None),
    )

    print(report.summary())
    for track in report.tracks:
        if track.stem_path:
            print(f"    stem  {track.stem_path}")
        if track.midi_path:
            print(f"    midi  {track.midi_path}")
    if report.mix_path:
        print(f"    mix   {report.mix_path}")
    if report.project_path:
        print(f"    proj  {report.project_path}")
    if report.midi_project_path:
        print(f"    proj  {report.midi_project_path}  (sequencing view)")

    # The spec is the primary artifact: it is what the artist edits, what
    # the library is approved from, and what a future session revises.
    spec_path = out_dir / "session.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    print(f"    spec  {spec_path}")

    # Measurements are the library's index: write them next to the session
    # so `library approve` can attach measured facts to each anchor.
    measurements_path = getattr(args, "measurements", None)
    if measurements_path:
        payload = {
            t.track_id: t.measurements.to_dict() for t in report.tracks
        }
        path = Path(measurements_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"    meas  {path}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdaw", description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--bit-depth", type=int, default=24)
    parser.add_argument("--no-drums", action="store_true", help="skip the context drum bed")
    parser.add_argument("--model", default=None, help="override the model id")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="show configuration status").set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="render the bundled session without an LLM")
    p.add_argument(
        "--render-dir",
        default=None,
        help="bake REAPER render settings into the project, writing audio here",
    )
    p.add_argument(
        "--measurements",
        default=None,
        help="write per-track measurements here (the patch library's index)",
    )
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("compose", help="produce a session from a description")
    p.add_argument("vision")
    p.add_argument("--library", default=None, help="approved patch library JSON")
    p.add_argument(
        "--render-dir",
        default=None,
        help="bake REAPER render settings into the project, writing audio here",
    )
    p.add_argument(
        "--measurements",
        default=None,
        help="write per-track measurements here (the patch library's index)",
    )
    p.set_defaults(func=cmd_compose)

    p = sub.add_parser(
        "library", help="approve, list and retrieve from the patch library"
    )
    p.add_argument("action", choices=["approve", "list", "find"])
    p.add_argument("query", nargs="?", default="", help="search text, for 'find'")
    p.add_argument("--library", default="patch-library.json", help="library JSON file")
    p.add_argument("--spec", default=None, help="session.json to approve patches from")
    p.add_argument(
        "--measurements", default=None, help="measurements JSON keyed by track id"
    )
    p.add_argument("--out-file", default=None, help="where to write the library")
    p.add_argument("--query", default="", help="search text")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_library)

    p = sub.add_parser("revise", help="apply an artist correction to a spec")
    p.add_argument("--spec", required=True)
    p.add_argument("--feedback", required=True)
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("preview", help="fast audition of a single patch")
    p.add_argument("--spec", required=True)
    p.add_argument("patch")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("measure", help="measure a wav file")
    p.add_argument("file")
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("generate", help="one-shot generative audio call")
    p.add_argument("--provider", required=True, choices=["stable_audio", "lyria"])
    p.add_argument("--prompt", required=True)
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--isolate", action="store_true", help="ask for one instrument, not a full arrangement")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("schema", help="export the JSON schemas")
    p.add_argument("--name", default=None)
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("reaper-render", help="render a .rpp through REAPER")
    p.add_argument("project", help="path to a .rpp")
    p.add_argument("--timeout", type=float, default=600.0)
    p.set_defaults(func=cmd_reaper_render)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
