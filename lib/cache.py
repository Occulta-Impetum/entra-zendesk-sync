"""Local cache helpers for Zendesk snapshots and incremental Entra state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = REPO_ROOT / "cache" / "zendesk_users.json"
ENTRA_CACHE_PATH = REPO_ROOT / "cache" / "entra_users.json"
CACHE_VERSION = 1
ENTRA_CACHE_VERSION = 1


class CacheError(RuntimeError):
    """Raised when a local cache cannot be read or written safely."""


def save_zendesk_users_cache(
    users: list[dict[str, Any]],
    *,
    subdomain: str,
    path: str | Path | None = None,
) -> Path:
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
        raise CacheError(f"Zendesk user cache version is unsupported: {payload.get('version')!r}")
    if str(payload.get("subdomain") or "").lower() != subdomain.lower():
        raise CacheError("Zendesk user cache belongs to a different subdomain and will not be used.")
    users = payload.get("users")
    if not isinstance(users, list):
        raise CacheError("Zendesk user cache does not contain a valid users list.")
    return users, {
        "path": cache_path,
        "fetched_at": str(payload.get("fetched_at") or "unknown"),
        "user_count": len(users),
    }


def load_entra_users_cache(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load incremental Entra state, including retained historical identities."""
    cache_path = Path(path) if path else ENTRA_CACHE_PATH
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CacheError(f"Unable to load Entra user cache from {cache_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != ENTRA_CACHE_VERSION:
        raise CacheError(f"Entra user cache version is unsupported or invalid: {cache_path}")
    current = payload.get("current")
    history = payload.get("history")
    if not isinstance(current, dict) or not isinstance(history, dict):
        raise CacheError("Entra user cache must contain current and history objects.")
    return payload


def save_entra_users_cache(
    current: dict[str, dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> Path:
    """Save current Entra state while retaining old users for email-reuse evidence."""
    cache_path = Path(path) if path else ENTRA_CACHE_PATH
    now = datetime.now(timezone.utc).isoformat()
    history: dict[str, dict[str, Any]] = {}
    if previous and isinstance(previous.get("history"), dict):
        history.update(previous["history"])
    if previous and isinstance(previous.get("current"), dict):
        history.update(previous["current"])
    history.update(current)

    payload = {
        "version": ENTRA_CACHE_VERSION,
        "saved_at": now,
        "current_count": len(current),
        "current": current,
        "history": history,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    except (OSError, TypeError, ValueError) as exc:
        raise CacheError(f"Unable to save Entra user cache to {cache_path}: {exc}") from exc
    return cache_path


def diff_entra_users(
    current: dict[str, dict[str, Any]],
    previous_cache: dict[str, Any] | None,
) -> tuple[set[str], set[str], set[str]]:
    """Return new, changed, and removed Entra IDs."""
    previous = previous_cache.get("current", {}) if previous_cache else {}
    if not isinstance(previous, dict):
        previous = {}
    current_ids = set(current)
    previous_ids = set(previous)
    new_ids = current_ids - previous_ids
    removed_ids = previous_ids - current_ids
    changed_ids = {
        user_id
        for user_id in current_ids & previous_ids
        if current[user_id] != previous[user_id]
    }
    return new_ids, changed_ids, removed_ids
