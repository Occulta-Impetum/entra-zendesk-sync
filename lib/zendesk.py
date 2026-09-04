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


def _scope_key(scope: str) -> str:
    return " ".join(sorted(set(scope.split())))


def _load_token_cache() -> dict[str, Any]:
    try:
        if not TOKEN_CACHE_PATH.exists():
            return {}
        data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_token_cache(cache: dict[str, Any]) -> None:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_access_token(
    config: dict[str, str] | None = None,
    *,
    scope: str | None = None,
    force_new: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Return a cached unexpired token or request a new client-credentials token."""
    config = config or load_zendesk_config()
    requested_scope = (scope or config["scope"]).strip()
    if not requested_scope:
        raise ZendeskError("Zendesk OAuth scope cannot be empty.")

    scope_key = _scope_key(requested_scope)
    cache_key = f"{config['subdomain']}|{config['client_id']}|{scope_key}"
    now = time.time()
    cache = _load_token_cache()
    cached = cache.get(cache_key) if isinstance(cache.get(cache_key), dict) else None
    if cached and not force_new:
        token = str(cached.get("access_token") or "")
        expires_at = float(cached.get("expires_at") or 0)
        if token and expires_at - TOKEN_EXPIRY_MARGIN_SECONDS > now:
            remaining = max(0, int((expires_at - now) / 60))
            print(
                f"      Reusing cached Zendesk OAuth token for exact scopes [{scope_key}] "
                f"(~{remaining} min remaining)."
            )
            return token, {
                "access_token": token,
                "scope": scope_key,
                "expires_in": max(0, int(expires_at - now)),
                "cached": True,
            }

    print(f"      Requesting a new Zendesk OAuth token for exact scopes [{scope_key}]...")
    url = f"https://{config['subdomain']}.zendesk.com/oauth/tokens"
    payload = {
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "scope": requested_scope,
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

    token = data.get("access_token")
    if not token:
        raise ZendeskError("Zendesk OAuth response did not contain an access token.")

    expires_in = int(data.get("expires_in") or 1800)
    cache[cache_key] = {
        "access_token": str(token),
        "scope": scope_key,
        "expires_at": now + expires_in,
    }
    try:
        _save_token_cache(cache)
    except OSError:
        pass
    print(
        f"      Cached new Zendesk OAuth token for exact scopes [{scope_key}] "
        f"(expires in ~{max(0, int(expires_in / 60) - 1)} min)."
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
    return zendesk_request(
        "GET",
        path_or_url,
        subdomain=subdomain,
        access_token=access_token,
        params=params,
    )


def get_user_fields(access_token: str, subdomain: str) -> list[dict[str, Any]]:
    """Return all Zendesk custom/standard user field definitions."""
    fields: list[dict[str, Any]] = []
    next_url = "user_fields.json"
    page = 0
    params: dict[str, str] | None = {"per_page": "100"}
    while next_url:
        page += 1
        print(
            f"      Fetching Zendesk user fields page {page} ({len(fields)} received so far)...",
            flush=True,
        )
        payload = zendesk_get(
            next_url,
            subdomain=subdomain,
            access_token=access_token,
            params=params,
        )
        params = None
        page_items = payload.get("user_fields", [])
        if not isinstance(page_items, list):
            raise ZendeskError("Zendesk user-fields response did not contain a list.")
        fields.extend(page_items)
        next_url = str(payload.get("next_page") or "")
    return fields


def create_user_field(
    access_token: str,
    subdomain: str,
    *,
    title: str,
    key: str,
    field_type: str = "text",
) -> dict[str, Any]:
    """Create one Zendesk user field and return its definition."""
    payload = {
        "user_field": {
            "title": title,
            "key": key,
            "type": field_type,
            "active": True,
        }
    }
    data = zendesk_request(
        "POST",
        "user_fields.json",
        subdomain=subdomain,
        access_token=access_token,
        json_body=payload,
    )
    field = data.get("user_field")
    if not isinstance(field, dict):
        raise ZendeskError("Zendesk create-user-field response did not contain a user_field object.")
    return field


def create_user(
    access_token: str,
    subdomain: str,
    *,
    name: str,
    email: str,
    external_id: str,
    organization_id: int,
    user_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_payload: dict[str, Any] = {
        "name": name,
        "email": email,
        "external_id": external_id,
        "organization_id": organization_id,
        "role": "end-user",
    }
    if user_fields:
        user_payload["user_fields"] = user_fields
    data = zendesk_request(
        "POST",
        "users.json",
        subdomain=subdomain,
        access_token=access_token,
        json_body={"user": user_payload},
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


def get_organizations(access_token: str, subdomain: str) -> list[dict[str, Any]]:
    organizations: list[dict[str, Any]] = []
    next_url = "organizations.json"
    page = 0
    params: dict[str, str] | None = {"per_page": "100"}
    while next_url:
        page += 1
        print(
            f"      Fetching organizations page {page} ({len(organizations)} received so far)...",
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
    return sorted(organizations, key=lambda org: (org.get("name") or "").lower())


def get_users(access_token: str, subdomain: str) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    next_url = "users.json"
    page = 0
    params: dict[str, str] | None = {"per_page": "100"}
    while next_url:
        page += 1
        print(
            f"      Fetching Zendesk users page {page} ({len(users)} received so far)...",
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
