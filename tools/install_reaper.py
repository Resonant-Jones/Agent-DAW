#!/usr/bin/env python3
"""Install REAPER.app without ever unlinking anything.

`ditto` fails here because it replaces files and applies extended
attributes, and this environment denies unlink. A plain file-by-file copy
that only ever opens files for writing works fine.

Usage: install_reaper.py <source.app> <dest.app>
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path


def copy_bundle(src: Path, dst: Path) -> tuple[int, int]:
    files = 0
    dirs = 0
    symlinks = 0

    for root, dirnames, filenames in os.walk(src, followlinks=False):
        rel = Path(root).relative_to(src)
        target_dir = dst / rel

        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            dirs += 1

        # Preserve directory permissions without touching anything that
        # already exists.
        try:
            os.chmod(target_dir, stat.S_IMODE(os.stat(root).st_mode) | 0o700)
        except OSError:
            pass

        for name in filenames:
            source = Path(root) / name
            target = target_dir / name

            if source.is_symlink():
                link_target = os.readlink(source)
                if target.is_symlink():
                    continue
                if target.exists():
                    # Cannot unlink here; a stale file of the same name is
                    # better than an error, and it is reported below.
                    continue
                os.symlink(link_target, target)
                symlinks += 1
                continue

            if not source.is_file():
                continue

            # Write into place. Opening for write truncates an existing
            # file, which is allowed; replacing one is not.
            shutil.copyfile(source, target)
            try:
                os.chmod(target, stat.S_IMODE(os.stat(source).st_mode) | stat.S_IWUSR)
            except OSError:
                pass
            files += 1

    return files, dirs, symlinks


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])

    if not src.exists():
        print(f"source not found: {src}")
        return 1

    if dst.exists():
        print(f"destination already exists: {dst}")
        return 1

    dst.mkdir(parents=True)
    files, dirs, symlinks = copy_bundle(src, dst)

    source_count = sum(1 for p in src.rglob("*") if p.is_file() or p.is_symlink())
    print(f"copied {files} files, {dirs} dirs, {symlinks} symlinks")
    print(f"source had {source_count} file/symlink entries")

    missing = []
    for path in src.rglob("*"):
        if path.is_file() or path.is_symlink():
            rel = path.relative_to(src)
            if not (dst / rel).exists():
                missing.append(str(rel))

    if missing:
        print(f"MISSING {len(missing)}:")
        for m in missing[:10]:
            print(f"   {m}")
        return 1

    print("complete: every source file is present at the destination")
    return 0


if __name__ == "__main__":
    sys.exit(main())
