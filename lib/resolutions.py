"""Persistent administrator decisions for reconciliation conflicts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESOLUTIONS_PATH = REPO_ROOT / "config" / "conflict_resolutions.yaml"


class ResolutionError(RuntimeError):
    """Raised when conflict resolutions cannot be loaded or saved."""


def load_resolutions(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load conflict decisions indexed by immutable Entra object ID."""
    resolution_path = Path(path) if path else DEFAULT_RESOLUTIONS_PATH
    if not resolution_path.exists():
        return {}

    try:
        with resolution_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ResolutionError(f"Unable to load conflict resolutions from {resolution_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ResolutionError("Conflict resolution file root must be a mapping.")

    decisions = data.get("resolutions", {})
    if not isinstance(decisions, dict):
        raise ResolutionError("Conflict resolution file 'resolutions' value must be a mapping.")

    result: dict[str, dict[str, Any]] = {}
    for entra_id, decision in decisions.items():
        if isinstance(decision, dict):
            result[str(entra_id)] = decision
    return result


def save_resolutions(
    resolutions: dict[str, dict[str, Any]],
    path: str | Path | None = None,
) -> Path:
    """Save administrator conflict decisions as non-secret YAML."""
    resolution_path = Path(path) if path else DEFAULT_RESOLUTIONS_PATH
    payload = {
        "version": 1,
        "resolutions": resolutions,
    }

    try:
        resolution_path.parent.mkdir(parents=True, exist_ok=True)
        with resolution_path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
    except (OSError, yaml.YAMLError) as exc:
        raise ResolutionError(f"Unable to save conflict resolutions to {resolution_path}: {exc}") from exc

    return resolution_path
