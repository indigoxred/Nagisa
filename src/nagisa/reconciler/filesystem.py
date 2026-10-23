"""Read-only containment, type and hardlink observations."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from nagisa.reconciler.models import FileObservation


def observe_file(root: Path, relative: PurePosixPath) -> FileObservation:
    reasons: set[str] = set()
    root = Path(root)
    raw = str(relative)
    if relative.is_absolute() or ".." in relative.parts or not raw or "\x00" in raw:
        return FileObservation(root, raw, None, False, False, None, None, ("unsafe-relative-path",))
    if not root.is_dir() or root.is_symlink():
        return FileObservation(root, raw, None, False, False, None, None, ("root-unhealthy",))
    candidate = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            reasons.add("symlink-component")
            break
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return FileObservation(root, raw, None, False, candidate.exists(), None, None,
                               tuple(sorted(reasons | {"missing-or-escaped"})))
    try:
        stat = os.stat(resolved, follow_symlinks=False)
    except OSError:
        return FileObservation(root, raw, resolved, False, False, None, None,
                               tuple(sorted(reasons | {"stat-failed"})))
    if not resolved.is_file():
        reasons.add("not-regular-file")
    if stat.st_nlink != 1:
        reasons.add("link-count-not-one")
    return FileObservation(
        root=root,
        relative=raw,
        resolved=resolved,
        safe=not reasons,
        exists=True,
        size=int(stat.st_size),
        link_count=int(stat.st_nlink),
        reasons=tuple(sorted(reasons)),
    )

