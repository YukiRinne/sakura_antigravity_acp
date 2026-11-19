"""Isolated path handling, Windows long-path normalization, and stale temp cleanup."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


def windows_long_path(path: Path) -> Path:
    text = str(path)
    if os.name == "nt":
        if text.startswith("\\\\?\\UNC\\"):
            normal = "\\\\" + text[8:]
            return Path(normal) if len(normal) < 248 else path
        if text.startswith("\\\\?\\"):
            normal = text[4:]
            return Path(normal) if len(normal) < 248 else path
        if path.is_absolute() and len(text) >= 248:
            if text.startswith("\\\\"):
                return Path("\\\\?\\UNC\\" + text[2:])
            return Path("\\\\?\\" + text)
    return path


def remove_tree_robust(path: Path) -> None:
    """Remove an isolated directory, retrying briefly on Windows handle-release lag."""
    target = windows_long_path(path)
    attempts = 6 if os.name == "nt" else 1
    for idx in range(attempts):
        if not target.exists():
            return
        if idx < attempts - 1:
            try:
                shutil.rmtree(target)
                return
            except OSError:
                time.sleep(0.03 * (idx + 1))
        else:
            shutil.rmtree(target, ignore_errors=True)


def cleanup_stale_dirs(
    root: Path,
    *,
    active_names: set[str] | frozenset[str] = frozenset(),
    min_age_seconds: float = 3600.0,
    now: float | None = None,
) -> int:
    """Clean stale temporary directories inside the plugin's own data directory."""
    root = windows_long_path(root)
    if not root.exists():
        return 0
    current = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            name = entry.name
            if name.startswith(("preflight-", "login-", "runtime-")):
                if (
                    name not in active_names
                    and current - entry.stat(follow_symlinks=False).st_mtime >= min_age_seconds
                ):
                    shutil.rmtree(entry.path, ignore_errors=True)
                    removed += 1
        except OSError:
            continue
    return removed
