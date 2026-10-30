#!/usr/bin/env python3
"""Verify a generated .rpp by having REAPER itself open and render it.

The strongest check on a generated REAPER project is not "the structure
looks right" -- it is REAPER loading the file and telling us what it saw.

REAPER runs ``__startup.lua`` (and ``__startup.eel``) from its resource
path at startup. This script installs a one-shot startup job that makes
REAPER open the generated project, write a report of what it made of it
(tracks, items, item lengths, media sources, project tempo and sample
rate), render it, and quit. The script then launches REAPER and reads the
report back.

Two environment facts shape this:

* **macOS does not pass command-line arguments to an application that is
  already running.** A second launch of a live REAPER is silently
  ignored, so REAPER has to be quit before this can work. The job is
  therefore designed to be run against a freshly started REAPER.
* **A REAPER process launched from a restricted sandbox cannot build its
  windows** (it needs windowserver XPC) and hangs at startup instead of
  failing loudly, so REAPER is launched through LaunchServices.

Usage:
    verify_reaper_handoff.py <project.rpp> [<render-dir>]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REAPER_APP = Path("/Applications/REAPER.app")
RESOURCE_DIR = Path.home() / "Library" / "Application Support" / "REAPER"

LUA_JOB = r'''
local JOB_FILE = "__JOB_FILE__"
if not JOB_FILE then return end

local function read_job(path)
  local job = {}
  local f = io.open(path, "r")
  if not f then return nil end
  for line in f:lines() do
    local key, value = line:match("^([^=]+)=(.*)$")
    if key then job[key] = value end
  end
  f:close()
  return job
end

local job = read_job(JOB_FILE)
if not job then return end
os.rename(JOB_FILE, JOB_FILE .. ".done")

local report = io.open(job.report, "w")
local function say(fmt, ...)
  local ok, line = pcall(string.format, fmt, ...)
  if not ok then line = tostring(fmt) end
  report:write(line .. "\n")
  report:flush()
end

say("Agent-DAW REAPER verification")
say("reaper version: %s", tostring(reaper.GetAppVersion and reaper.GetAppVersion() or "unknown"))

reaper.Main_openProject(job.project)
say("opened project: %s", tostring(job.project))
say("project file: %s", tostring(reaper.GetActiveProjectFilename and reaper.GetActiveProjectFilename() or "?"))
say("tempo: %.4f", reaper.GetTempoTimeSig())
say("sample rate: %d", reaper.GetSetProjectInfo(0, "PROJECT_SRATE", 0, false))
say("track count: %d", reaper.CountTracks(0))

for i = 0, reaper.CountTracks(0) - 1 do
  local track = reaper.GetTrack(0, i)
  local items = reaper.CountTrackMediaItems(track)
  say("track %d: %q  items=%d", i, reaper.GetTrackName(track, ""), items)
  for j = 0, items - 1 do
    local item = reaper.GetTrackMediaItem(track, j)
    local take = reaper.GetActiveTake(item)
    local source_type, source_name = "?", ""
    if take then
      local src = reaper.GetMediaItemTake_Source(take)
      if src then
        source_type = reaper.GetMediaSourceType(src, "")
        source_name = reaper.GetMediaSourceFileName(src, "")
      end
    end
    say("  item %d: length=%.4f position=%.4f source=%s %q",
      j,
      reaper.GetMediaItemLength(item),
      reaper.GetMediaItemInfo_Value(item, "D_POSITION"),
      source_type,
      source_name)
  end
end

if job.render_dir then
  -- The project carries its own render settings (RENDER_FILE / RENDER_PATTERN).
  -- Nothing is overridden here on purpose: the point is to render the
  -- project exactly as it was written.
  say("render requested: dir=%s pattern=%s", job.render_dir, tostring(job.pattern))
  reaper.Main_OnCommand(41824, 0)
  say("render finished")
end

say("done")
report:close()
reaper.Main_OnCommand(40004, 0)
'''

EEL_MARKER = """
handle = file_open("__MARKER__", "w");
file_var(%handle, 0, "agentdaw startup script ran\\n");
file_close(%handle);
"""


def install_job(job_file: Path, script_dir: Path) -> None:
    """Put the startup script where REAPER will find it.

    The job path is baked into the script rather than passed in the
    environment: REAPER is launched through LaunchServices, which does not
    inherit this process's environment.
    """
    lua = LUA_JOB.replace("__JOB_FILE__", str(job_file))
    for target in (
        RESOURCE_DIR / "Scripts",
        RESOURCE_DIR / "profiles" / "agentdaw" / "Scripts",
    ):
        target.mkdir(parents=True, exist_ok=True)
        (target / "__startup.lua").write_text(lua, encoding="utf-8")
        (target / "__startup.eel").write_text(
            EEL_MARKER.replace("__MARKER__", str(job_file.parent / "startup_marker.txt")),
            encoding="utf-8",
        )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    project = Path(sys.argv[1]).resolve()
    if not project.exists():
        print(f"project not found: {project}")
        return 1

    work = project.parent / "reaper_verify"
    work.mkdir(parents=True, exist_ok=True)

    # Take the render target from the project itself, so the render runs
    # exactly the settings the project was written with.
    render_dir = ""
    pattern = "render"
    for line in project.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("RENDER_FILE "):
            render_dir = stripped.split('"')[1]
        elif stripped.startswith("RENDER_PATTERN "):
            pattern = stripped.split('"')[1]
    if render_dir:
        Path(render_dir).mkdir(parents=True, exist_ok=True)

    job_file = work / "job.txt"
    report_file = work / "report.txt"
    job_file.write_text(
        f"project={project}\n"
        f"render_dir={render_dir}\n"
        f"pattern={pattern}\n"
        f"report={report_file}\n",
        encoding="utf-8",
    )

    install_job(job_file, work)

    env = dict(os.environ)
    env["AGENTDAW_JOB_FILE"] = str(job_file)

    command = [
        "open", "-a", str(REAPER_APP), "--args", "-nosplash", "-profile", "agentdaw",
    ]
    print(f"launching: {' '.join(command)}")
    subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if report_file.exists():
            time.sleep(1.0)
            print("\n--- REAPER report ---")
            print(report_file.read_text())
            print("--- rendered files ---")
            found = sorted(p for p in render_dir.iterdir() if p.is_file())
            for p in found:
                print(f"  {p.name}  {p.stat().st_size} bytes")
            if not found:
                print("  (none)")
            return 0 if found else 1
        time.sleep(2.0)

    print("no report: REAPER did not run the startup script.")
    print("Most likely REAPER was already running (macOS drops arguments for")
    print("a running app) or its first-run screen is waiting for a click.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
