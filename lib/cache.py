"""Local cache helpers for expensive read-only Zendesk discovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = REPO_ROOT / "cache" / "zendesk_users.json"
CACHE_VERSION = 1


class CacheError(RuntimeError):
    """Raised when a local cache cannot be read or written safely."""


def save_zendesk_users_cache(
    users: list[dict[str, Any]],
    *,
    subdomain: str,
    path: str | Path | None = None,
) -> Path:
    """Persist a local snapshot of Zendesk users for repeat dry-run testing."""
    cache_path = Path(path) if path else DEFAULT_CACHE_PATH
    payload = {
        "version": CACHE_VERSION,
        "subdomain": subdomain,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "user_count": len(users),
        "users": users,
    }

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as exc:
        raise CacheError(f"Unable to save Zendesk user cache to {cache_path}: {exc}") from exc

    return cache_path


def load_zendesk_users_cache(
    *,
    subdomain: str,
    path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Load a compatible Zendesk user snapshot, or return None if none exists."""
    cache_path = Path(path) if path else DEFAULT_CACHE_PATH
    if not cache_path.exists():
        return None

    try:
        with cache_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CacheError(f"Unable to load Zendesk user cache from {cache_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise CacheError(f"Zendesk user cache root must be an object: {cache_path}")
    if payload.get("version") != CACHE_VERSION:
        raise CacheError(
            f"Zendesk user cache version is unsupported: {payload.get('version')!r}"
        )
    if str(payload.get("subdomain") or "").lower() != subdomain.lower():
        raise CacheError(
            "Zendesk user cache belongs to a different subdomain and will not be used."
        )

    users = payload.get("users")
    if not isinstance(users, list):
        raise CacheError("Zendesk user cache does not contain a valid users list.")

    metadata = {
        "path": cache_path,
        "fetched_at": str(payload.get("fetched_at") or "unknown"),
        "user_count": len(users),
    }
    return users, metadata
