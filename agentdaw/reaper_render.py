"""Render a project through REAPER, headlessly.

REAPER's project format is the handoff target, so the strongest possible
verification of a generated .rpp is: hand it to REAPER and make it render.

    REAPER -nosplash -profile <name> -renderproject <project.rpp>

Two things about the environment shape this module.

**REAPER is launched through `open`, not executed directly.** It is a
Cocoa app, and a Cocoa app needs windowserver XPC services to build its
windows; a process running inside a restricted sandbox cannot reach them,
so the app hangs at startup instead of failing loudly. Launching through
LaunchServices puts it in the user's login session, where it belongs.

**A dedicated profile is seeded, and the instance is forced.** macOS will
not hand command-line arguments to an application that is already running,
so a second `open` of REAPER would silently do nothing. `-newinst` plus
`open -n` forces a separate process, and the profile directory carries an
ini that stops REAPER from putting a modal dialog in front of the render.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

REAPER_BINARY = Path("/Applications/REAPER.app/Contents/MacOS/REAPER")
REAPER_RESOURCE_DIR = Path.home() / "Library" / "Application Support" / "REAPER"

# Keys that keep REAPER's startup non-interactive.
#
# audiocfgopen=0  stops the audio-device dialog from opening at startup.
# vst_scan=2      stops the VST scan at startup (we load no plugins).
#
# The coreaudio* keys name the output device REAPER should use. They are
# deliberately left unset: naming a device that CoreAudio does not have
# makes REAPER fail with "Error opening CoreAudio device" and park on a
# modal dialog. This machine reports no CoreAudio devices at all, so the
# device has to be chosen once in REAPER's own Audio preferences, which
# writes the setting where REAPER expects it.
_UNATTENDED_INI = """[reaper]
audiocfgopen=0
vst_scan=2
coreaudiosrate=44100
coreaudiosrateuse=1
coreaudiobs=0
coreaudiobsuse=0
coreaudioignorereset=0
coreaudioignprojsr=0
"""


class ReaperError(RuntimeError):
    pass


def find_reaper() -> Path | None:
    """Locate the REAPER binary."""
    if REAPER_BINARY.exists():
        return REAPER_BINARY
    for candidate in (
        Path("/Applications/REAPER.app/Contents/MacOS/REAPER"),
        Path.home() / "Applications/REAPER.app/Contents/MacOS/REAPER",
    ):
        if candidate.exists():
            return candidate
    return None


def ensure_profile(name: str = "agentdaw") -> Path:
    """Create and seed the REAPER profile directory.

    REAPER prompts for a resource path when a profile does not exist, and
    that prompt blocks forever in a headless context. Pre-creating the
    directory turns the prompt into a no-op; seeding the ini stops the
    audio-device and VST-scan dialogs from taking its place.

    An ini that already has content is left alone: once REAPER has run
    successfully its own settings are worth more than ours.
    """
    profile_dir = REAPER_RESOURCE_DIR / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)

    ini = profile_dir / "reaper.ini"
    if not ini.exists() or ini.stat().st_size == 0:
        ini.write_text(_UNATTENDED_INI, encoding="utf-8")
    return profile_dir


def render_project(
    project_path: str | Path,
    output_dir: str | Path,
    pattern: str = "render",
    profile: str = "agentdaw",
    timeout: float = 600.0,
    poll_interval: float = 1.0,
) -> Path:
    """Render a .rpp with REAPER and return the rendered file."""
    reaper = find_reaper()
    if reaper is None:
        raise ReaperError("REAPER not found at /Applications/REAPER.app")

    project_path = Path(project_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not project_path.exists():
        raise ReaperError(f"project not found: {project_path}")

    ensure_profile(profile)

    audio_suffixes = (".wav", ".flac", ".mp3", ".aif", ".aiff", ".ogg")

    def produced() -> list[Path]:
        return sorted(
            p for p in output_dir.iterdir()
            if p.is_file() and p.suffix.lower() in audio_suffixes
        )

    before = {p.name for p in produced()}

    # `open -n` asks LaunchServices for a *new* instance. Without it,
    # macOS activates the running REAPER and drops the arguments, and the
    # render silently never happens.
    #
    # -ApplePersistence NO goes last: it is a Cocoa argument, and Cocoa
    # stops parsing its own switches after the first non-Cocoa one.
    command = [
        "open",
        "-n",
        "-a", str(reaper),
        "--args",
        "-newinst",
        "-nosplash",
        "-ignoreerrors",
        "-profile", profile,
        "-renderproject", str(project_path),
        "-ApplePersistence", "NO",
    ]

    try:
        subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise ReaperError("could not hand the render to REAPER") from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        new = [p for p in produced() if p.name not in before]
        if new:
            # Let the encoder finish flushing.
            time.sleep(0.5)
            return max(new, key=lambda p: p.stat().st_mtime)

    raise ReaperError(
        f"REAPER produced no audio within {timeout:.0f}s in {output_dir}. "
        "If REAPER has never been launched on this machine, it needs its "
        "one-time interactive first-run screen dismissed before it will "
        "render unattended."
    )
