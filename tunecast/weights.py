"""Idempotent model-weight provisioning: verify by size manifest, download only when needed."""

from __future__ import annotations

import json
import logging
import time
from importlib.resources import files
from pathlib import Path

from huggingface_hub import snapshot_download

from .config import Settings
from .log import log_event

MARKER = ".tunecast_complete"


class WeightsError(Exception):
    """The weight tree is still incomplete after a download attempt."""


def load_manifest() -> list[tuple[str, int]]:
    """(relative path, byte size) for every file in the pinned snapshot."""
    raw = files("tunecast").joinpath("weights_manifest.json").read_text(encoding="utf-8")
    return [(entry["path"], int(entry["size"])) for entry in json.loads(raw)["files"]]


def verify(models_dir: Path, revision: str) -> list[str]:
    """Relative paths that are missing or have the wrong size; the marker counts as a path. Empty means complete."""
    bad: list[str] = []
    try:
        marker_ok = (models_dir / MARKER).read_text(encoding="utf-8").strip() == revision
    except OSError:
        marker_ok = False
    if not marker_ok:
        bad.append(MARKER)
    for rel, expected in load_manifest():
        try:
            actual = (models_dir / rel).stat().st_size
        except OSError:
            bad.append(rel)
            continue
        if actual != expected:
            bad.append(rel)
    return bad


def write_marker(models_dir: Path, revision: str) -> None:
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / MARKER).write_text(revision + "\n", encoding="utf-8")


def ensure_weights(settings: Settings, logger: logging.Logger) -> float:
    """Make the weight tree complete. Returns seconds spent downloading, 0.0 when nothing was needed."""
    missing = verify(settings.models_dir, settings.model_revision)
    if not missing:
        log_event(logger, "weights_present", path=str(settings.models_dir), revision=settings.model_revision)
        return 0.0

    log_event(
        logger,
        "weights_download_start",
        repo_id=settings.model_id,
        revision=settings.model_revision,
        missing_files=len(missing),
        path=str(settings.models_dir),
    )
    started = time.monotonic()
    snapshot_download(
        repo_id=settings.model_id,
        revision=settings.model_revision,
        local_dir=str(settings.models_dir),
        token=settings.hf_token,
    )
    elapsed = time.monotonic() - started

    still_bad = [rel for rel in verify(settings.models_dir, settings.model_revision) if rel != MARKER]
    if still_bad:
        shown = ", ".join(still_bad[:10]) + (" ..." if len(still_bad) > 10 else "")
        raise WeightsError(f"weights incomplete after download ({len(still_bad)} files): {shown}")

    write_marker(settings.models_dir, settings.model_revision)
    log_event(logger, "weights_download_done", elapsed_s=round(elapsed, 1), path=str(settings.models_dir))
    return elapsed
