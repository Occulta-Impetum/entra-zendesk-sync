"""Bootstrap identity-review snapshots and administrator decisions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REVIEW_PATH = REPO_ROOT / "cache" / "bootstrap_review.json"
DEFAULT_DECISIONS_PATH = REPO_ROOT / "config" / "bootstrap_review_resolutions.yaml"


class BootstrapReviewError(RuntimeError):
    """Raised when bootstrap review data cannot be loaded or saved."""


def build_review_candidates(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return email-bootstrap rows whose existing Zendesk name differs from Entra/HR."""
    candidates: list[dict[str, Any]] = []
    for row in plan:
        action = str(row.get("action") or "")
        matched_by = str(row.get("matched_by") or "")
        if matched_by != "email" or "UPDATE NAME" not in action:
            continue
        if "RELINK" in action:
            review_type = "relink_name_mismatch"
        elif "ADOPT" in action:
            review_type = "adopt_name_mismatch"
        else:
            continue
        candidate = dict(row)
        candidate["review_type"] = review_type
        candidates.append(candidate)
    return candidates


def save_review_candidates(
    candidates: list[dict[str, Any]], path: str | Path | None = None
) -> Path:
    review_path = Path(path) if path else DEFAULT_REVIEW_PATH
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reviews": candidates,
    }
    try:
        review_path.parent.mkdir(parents=True, exist_ok=True)
        with review_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as exc:
        raise BootstrapReviewError(f"Unable to save bootstrap review snapshot to {review_path}: {exc}") from exc
    return review_path


def load_review_candidates(path: str | Path | None = None) -> list[dict[str, Any]]:
    review_path = Path(path) if path else DEFAULT_REVIEW_PATH
    if not review_path.exists():
        return []
    try:
        with review_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapReviewError(f"Unable to load bootstrap review snapshot from {review_path}: {exc}") from exc
    reviews = payload.get("reviews", []) if isinstance(payload, dict) else []
    if not isinstance(reviews, list):
        raise BootstrapReviewError("Bootstrap review snapshot 'reviews' value must be a list.")
    return [item for item in reviews if isinstance(item, dict)]


def load_review_decisions(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    decisions_path = Path(path) if path else DEFAULT_DECISIONS_PATH
    if not decisions_path.exists():
        return {}
    try:
        with decisions_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BootstrapReviewError(f"Unable to load bootstrap review decisions from {decisions_path}: {exc}") from exc
    decisions = payload.get("decisions", {}) if isinstance(payload, dict) else {}
    if not isinstance(decisions, dict):
        raise BootstrapReviewError("Bootstrap review decision file 'decisions' value must be a mapping.")
    return {
        str(entra_id): decision
        for entra_id, decision in decisions.items()
        if isinstance(decision, dict)
    }


def save_review_decisions(
    decisions: dict[str, dict[str, Any]], path: str | Path | None = None
) -> Path:
    decisions_path = Path(path) if path else DEFAULT_DECISIONS_PATH
    payload = {"version": 1, "decisions": decisions}
    try:
        decisions_path.parent.mkdir(parents=True, exist_ok=True)
        with decisions_path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
    except (OSError, yaml.YAMLError) as exc:
        raise BootstrapReviewError(f"Unable to save bootstrap review decisions to {decisions_path}: {exc}") from exc
    return decisions_path


def unresolved_review_candidates(
    candidates: list[dict[str, Any]], decisions: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for candidate in candidates:
        entra_id = str(candidate.get("entra_id") or "")
        decision = str((decisions.get(entra_id) or {}).get("decision") or "")
        if decision not in {"approve_hr_name", "manual_review"}:
            unresolved.append(candidate)
    return unresolved
