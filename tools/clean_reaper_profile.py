"""Strip the bogus CoreAudio device names from REAPER's profile ini.

Naming a device that CoreAudio does not have makes REAPER fail with
"Error opening CoreAudio device" and park on a modal dialog at startup.
"""

import sys
from pathlib import Path

DROP = {"coreaudiooutdevnew", "coreaudioindevnew"}

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()

out: list[str] = []
in_reaper = False
removed: list[str] = []
for line in lines:
    stripped = line.strip()
    if stripped.startswith("["):
        in_reaper = stripped.lower() == "[reaper]"
    if in_reaper and "=" in stripped:
        key = stripped.split("=", 1)[0].strip()
        if key in DROP:
            removed.append(key)
            continue
    out.append(line)

path.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"removed {removed or 'nothing'} from {path}")
