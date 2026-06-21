"""Safe deletion of temporary post image files."""

import logging
import os

logger = logging.getLogger(__name__)


def cleanup_files(paths: list[str]) -> None:
    """Delete temp files; ignore missing paths and log failures without raising."""
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        unique.append(norm)

    if not unique:
        return

    logger.info("TEMP FILE CLEANUP START")
    for path in unique:
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            logger.info("FILE DELETED: %s", path)
        except OSError as exc:
            logger.warning("FILE DELETE FAILED: %s (%s)", path, exc)


def cleanup_paths(*paths: str | None) -> None:
    """Backward-compatible wrapper for single or multiple paths."""
    cleanup_files([p for p in paths if p])
