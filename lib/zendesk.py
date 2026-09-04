"""Zendesk OAuth and API helpers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

DEFAULT_SCOPE = "organizations:read"
REQUEST_TIMEOUT = 30
TOKEN_EXPIRY_MARGIN_SECONDS = 60
REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_CACHE_PATH = REPO_ROOT / "cache" / "zendesk_oauth_tokens.json"


class ZendeskError(RuntimeError):
    """Raised when Zendesk configuration, authentication, or API calls fail."""


def load_zendesk_config() -> dict[str, str]:
    """Load and validate Zendesk OAuth configuration from .env/environment."""
    # Project-local .env values are authoritative so stale shell/VS Code
    # environment variables cannot silently override the current configuration.
    load_dotenv(override=True)

    config = {
        "subdomain": (os.getenv("ZENDESK_SUBDOMAIN") or "").strip(),
        "client_id": (os.getenv("ZENDESK_OAUTH_CLIENT_ID") or "").strip(),
        "client_secret": (os.getenv("ZENDESK_OAUTH_CLIENT_SECRET") or "").strip(),
        "scope": (os.getenv("ZENDESK_OAUTH_SCOPE") or DEFAULT_SCOPE).strip(),
    }

    missing = [
        name
        for name, value in (
            ("ZENDESK_SUBDOMAIN", config["subdomain"]),
            ("ZENDESK_OAUTH_CLIENT_ID", config["client_id"]),
            ("ZENDESK_OAUTH_CLIENT_SECRET", config["client_secret"]),
        )
        if not value
    ]
    if missing:
        raise ZendeskError(
            "Zendesk configuration is incomplete. Missing: " + ", ".join(missing)
        )

    subdomain = config["subdomain"]
    subdomain = subdomain.removeprefix("https://").removeprefix("http://")
    subdomain = subdomain.split(".", 1)[0].strip("/")
    if not subdomain:
        raise ZendeskError("ZENDESK_SUBDOMAIN is empty after normalization.")

    config["subdomain"] = subdomain
    return config


def _safe_error_detail(response: requests.Response, secret: str = "") -> str:
    """Return useful API error text without exposing credentials."""
    try:
        body = response.json()
        if isinstance(body, dict):
            candidates = (
                body.get("error_description"),
                body.get("description"),
                body.get("message"),
                body.get("error"),
            )
            detail = next((str(value) for value in candidates if value), str(body))
        else:
            detail = str(body)
    except ValueError:
        detail = (response.text or "").strip()

    if secret and detail:
        detail = detail.replace(secret, "[REDACTED]")
    return " ".join(detail.split())[:750]


def _normalize_scope(scope: str) -> str:
    """Normalize an OAuth scope string so equivalent scope sets share one cache entry."""
    return " ".join(sorted({part for part in scope.split() if part}))


def _token_cache_key(config: dict[str, str], normalized_scope: str) -> str:
    """Return a stable non-secret key for one tenant/client/exact scope set."""
    return f"{config['subdomain'].lower()}|{config['client_id']}|{normalized_scope}"


def _load_token_cache() -> dict[str, Any]:
    """Load the local OAuth cache; a corrupt cache is ignored safely."""
    try:
        with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"version": 1, "entries": {}}
    except (OSError, ValueError, TypeError):
        return {"version": 1, "entries": {}}

    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"version": 1, "entries": {}}
    return data


def _save_token_cache(cache: dict[str, Any]) -> None:
    """Atomically save cached OAuth tokens under the already-gitignored cache folder."""
    try:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = TOKEN_CACHE_PATH.with_suffix(TOKEN_CACHE_PATH.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, TOKEN_CACHE_PATH)
    except OSError as exc:
        # A cache failure must not prevent authentication. The caller can still
        # use the newly issued in-memory token for this run.
        print(f"      WARNING: Unable to save Zendesk OAuth token cache: {exc}")


def _get_cached_token(
    config: dict[str, str],
    normalized_scope: str,
) -> tuple[str, dict[str, Any]] | None:
    """Return a still-valid token for the exact requested scope set, if available."""
    cache = _load_token_cache()
    key = _token_cache_key(config, normalized_scope)
    entry = cache.get("entries", {}).get(key)
    if not isinstance(entry, dict):
        return None

    token = str(entry.get("access_token") or "")
    try:
        expires_at = float(entry.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None

    now = time.time()
    if not token or expires_at <= now + TOKEN_EXPIRY_MARGIN_SECONDS:
        return None

    seconds_left = max(0, int(expires_at - now))
    print(
        "      Reusing cached Zendesk OAuth token "
        f"for exact scopes [{normalized_scope}] (~{seconds_left // 60} min remaining)."
    )
    token_data = {
        "access_token": token,
        "token_type": entry.get("token_type") or "bearer",
        "expires_in": seconds_left,
        "scope": entry.get("scope") or normalized_scope,
        "cached": True,
    }
    return token, token_data


def _cache_token(
    *,
    config: dict[str, str],
    normalized_scope: str,
    token: str,
    token_data: dict[str, Any],
) -> None:
    """Cache an expiring client-credentials token for reuse across script runs."""
    try:
        expires_in = int(token_data.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0

    if expires_in <= TOKEN_EXPIRY_MARGIN_SECONDS:
        print(
            "      Zendesk OAuth response did not provide a reusable expiration window; "
            "this token will be used only for the current process."
        )
        return

    cache = _load_token_cache()
    entries = cache.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        cache["entries"] = entries

    key = _token_cache_key(config, normalized_scope)
    granted_scope = _normalize_scope(str(token_data.get("scope") or normalized_scope))
    entries[key] = {
        "access_token": token,
        "expires_at": time.time() + expires_in,
        "scope": granted_scope,
        "token_type": str(token_data.get("token_type") or "bearer"),
        "subdomain": config["subdomain"],
        "client_id": config["client_id"],
    }
    cache["version"] = 1
    _save_token_cache(cache)
    print(
        "      Cached new Zendesk OAuth token "
        f"for exact scopes [{normalized_scope}] (expires in ~{expires_in // 60} min)."
    )


def get_access_token(
    config: dict[str, str] | None = None,
    *,
    scope: str | None = None,
    force_new: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Return a Zendesk client-credentials access token.

    Tokens are cached locally and reused until shortly before expiration. Cache
    entries are keyed by tenant, OAuth client, and the *exact* requested scope
    set so a broader token is never silently substituted for a least-privilege
    request. Client-credentials tokens have no refresh token; once a cached
    token expires, this function repeats the client-credentials request.

    ``ZENDESK_OAUTH_SCOPE`` is only the default. Callers should pass an explicit
    scope whenever practical so read-only runs never request write permissions.
    """
    config = config or load_zendesk_config()
    requested_scope = (scope or config["scope"]).strip()
    normalized_scope = _normalize_scope(requested_scope)
    if not normalized_scope:
        raise ZendeskError("Zendesk OAuth scope cannot be empty.")

    if not force_new:
        cached = _get_cached_token(config, normalized_scope)
        if cached is not None:
            return cached

    print(
        "      Requesting a new Zendesk OAuth token "
        f"for exact scopes [{normalized_scope}]...",
        flush=True,
    )
    url = f"https://{config['subdomain']}.zendesk.com/oauth/tokens"
    payload = {
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "scope": normalized_scope,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise ZendeskError(f"Could not contact Zendesk OAuth endpoint: {exc}") from exc

    if not response.ok:
        detail = _safe_error_detail(response, config["client_secret"])
        suffix = f" Response: {detail}" if detail else ""
        raise ZendeskError(
            f"Zendesk OAuth request failed with HTTP {response.status_code}.{suffix}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise ZendeskError("Zendesk returned an invalid OAuth response.") from exc

    if not isinstance(data, dict):
        raise ZendeskError("Zendesk returned an invalid OAuth response object.")

    token = data.get("access_token")
    if not token:
        raise ZendeskError("Zendesk OAuth response did not contain an access token.")

    # Preserve an explicit scope value for callers/logging even if Zendesk omits
    # it from an otherwise valid response.
    data.setdefault("scope", normalized_scope)
    _cache_token(
        config=config,
        normalized_scope=normalized_scope,
        token=str(token),
        token_data=data,
    )
    return str(token), data


def zendesk_request(
    method: str,
    path_or_url: str,
    *,
    subdomain: str,
    access_token: str,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform an authenticated Zendesk API request and return JSON when present."""
    url = (
        path_or_url
        if path_or_url.lower().startswith("https://")
        else f"https://{subdomain}.zendesk.com/api/v2/{path_or_url.lstrip('/')}"
    )

    try:
        response = requests.request(
            method.upper(),
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params=params,
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ZendeskError(f"Zendesk API request failed: {exc}") from exc

    if not response.ok:
        detail = _safe_error_detail(response)
        suffix = f" Response: {detail}" if detail else ""
        raise ZendeskError(
            f"{method.upper()} {response.url} failed with HTTP {response.status_code}.{suffix}"
        )

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise ZendeskError(f"Zendesk returned invalid JSON from {response.url}") from exc


def zendesk_get(
    path_or_url: str,
    *,
    subdomain: str,
    access_token: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform an authenticated GET request against the Zendesk API."""
    return zendesk_request(
        "GET",
        path_or_url,
        subdomain=subdomain,
        access_token=access_token,
        params=params,
    )


def create_user(
    access_token: str,
    subdomain: str,
    *,
    name: str,
    email: str,
    external_id: str,
    organization_id: int,
) -> dict[str, Any]:
    """Create one Zendesk end user for an Entra-managed identity."""
    payload = {
        "user": {
            "name": name,
            "email": email,
            "external_id": external_id,
            "organization_id": organization_id,
            "role": "end-user",
        }
    }
    data = zendesk_request(
        "POST",
        "users.json",
        subdomain=subdomain,
        access_token=access_token,
        json_body=payload,
    )
    user = data.get("user")
    if not isinstance(user, dict) or user.get("id") is None:
        raise ZendeskError("Zendesk create-user response did not contain a user id.")
    return user


def update_user(
    access_token: str,
    subdomain: str,
    user_id: int,
    *,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Update writable fields on one Zendesk user."""
    if not fields:
        return {}
    data = zendesk_request(
        "PUT",
        f"users/{int(user_id)}.json",
        subdomain=subdomain,
        access_token=access_token,
        json_body={"user": fields},
    )
    user = data.get("user")
    if not isinstance(user, dict):
        raise ZendeskError(f"Zendesk update response for user {user_id} did not contain a user object.")
    return user


def get_organizations(
    access_token: str,
    subdomain: str,
) -> list[dict[str, Any]]:
    """Return all Zendesk organizations with visible pagination progress."""
    organizations: list[dict[str, Any]] = []
    next_url = "organizations.json"
    page = 0
    params: dict[str, str] | None = {"per_page": "100"}

    while next_url:
        page += 1
        print(
            f"      Fetching organizations page {page} "
            f"({len(organizations)} received so far)...",
            flush=True,
        )
        payload = zendesk_get(
            next_url,
            subdomain=subdomain,
            access_token=access_token,
            params=params,
        )
        params = None
        page_items = payload.get("organizations", [])
        if not isinstance(page_items, list):
            raise ZendeskError("Zendesk organizations response did not contain a list.")
        organizations.extend(page_items)
        next_url = payload.get("next_page") or ""

    return sorted(
        organizations,
        key=lambda org: (org.get("name") or "").lower(),
    )


def get_users(
    access_token: str,
    subdomain: str,
) -> list[dict[str, Any]]:
    """Return all Zendesk users needed for reconciliation.

    The sync needs the complete user set so it can match by external_id/email
    and identify already-linked users who have left all configured Entra groups.
    """
    users: list[dict[str, Any]] = []
    next_url = "users.json"
    page = 0
    params: dict[str, str] | None = {"per_page": "100"}

    while next_url:
        page += 1
        print(
            f"      Fetching Zendesk users page {page} "
            f"({len(users)} received so far)...",
            flush=True,
        )
        payload = zendesk_get(
            next_url,
            subdomain=subdomain,
            access_token=access_token,
            params=params,
        )
        params = None
        page_items = payload.get("users", [])
        if not isinstance(page_items, list):
            raise ZendeskError("Zendesk users response did not contain a list.")
        users.extend(page_items)
        next_url = payload.get("next_page") or ""

    return users
