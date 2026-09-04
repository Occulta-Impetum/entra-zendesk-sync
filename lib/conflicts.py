"""Helpers for persisting the latest unresolved conflict snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFLICTS_PATH = REPO_ROOT / "cache" / "conflicts.json"


class ConflictSnapshotError(RuntimeError):
    """Raised when conflict snapshots cannot be loaded or saved."""


def save_conflicts(conflicts: list[dict[str, Any]], path: str | Path | None = None) -> Path:
    conflict_path = Path(path) if path else DEFAULT_CONFLICTS_PATH
    payload = {"version": 1, "conflicts": conflicts}
    try:
        conflict_path.parent.mkdir(parents=True, exist_ok=True)
        with conflict_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as exc:
        raise ConflictSnapshotError(f"Unable to save conflict snapshot to {conflict_path}: {exc}") from exc
    return conflict_path


def load_conflicts(path: str | Path | None = None) -> list[dict[str, Any]]:
    conflict_path = Path(path) if path else DEFAULT_CONFLICTS_PATH
    if not conflict_path.exists():
        return []
    try:
        with conflict_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConflictSnapshotError(f"Unable to load conflict snapshot from {conflict_path}: {exc}") from exc
    conflicts = payload.get("conflicts", []) if isinstance(payload, dict) else []
    if not isinstance(conflicts, list):
        raise ConflictSnapshotError("Conflict snapshot 'conflicts' value must be a list.")
    return [item for item in conflicts if isinstance(item, dict)]
