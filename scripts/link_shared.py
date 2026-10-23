#!/usr/bin/env python3
"""Replace project template files that match their shared/ counterpart with relative symlinks to it.

Only files byte-identical to shared/ with the same executable bit are replaced, so every read stays the same and no dry run is needed; others are reported as diverged and left alone.
"""

from __future__ import annotations

import filecmp
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_ROOT = os.path.join(REPO_ROOT, "projects")
SHARED_ROOT = os.path.join(REPO_ROOT, "shared")


def is_executable(path: str) -> bool:
    return bool(os.stat(path).st_mode & 0o111)


def shared_relpaths() -> list[str]:
    result = []
    for dirpath, _, filenames in os.walk(SHARED_ROOT):
        for name in filenames:
            if name != ".DS_Store":
                result.append(os.path.relpath(os.path.join(dirpath, name), SHARED_ROOT))
    return sorted(result)


def main() -> None:
    templates = sorted(
        entry.path for entry in os.scandir(PROJECTS_ROOT) if entry.is_dir(follow_symlinks=False)
    )
    relpaths = shared_relpaths()
    for template in templates:
        for rel in relpaths:
            source = os.path.join(SHARED_ROOT, rel)
            path = os.path.join(template, rel)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            shown = os.path.relpath(path, REPO_ROOT)
            if filecmp.cmp(path, source, shallow=False) and is_executable(path) == is_executable(source):
                os.unlink(path)
                os.symlink(os.path.relpath(source, os.path.dirname(path)), path)
                print(f"linked    {shown}")
            else:
                print(f"diverged  {shown}")


if __name__ == "__main__":
    main()
