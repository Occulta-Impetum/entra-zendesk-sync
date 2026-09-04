"""Zendesk OAuth and API helpers."""

from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

DEFAULT_SCOPE = "organizations:read"
REQUEST_TIMEOUT = 30


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


def get_access_token(
    config: dict[str, str] | None = None,
    *,
    scope: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Request a short-lived Zendesk OAuth token using client credentials.

    ``ZENDESK_OAUTH_SCOPE`` is only the default. Callers can pass an explicit
    scope so read-only runs never request write permissions.
    """
    config = config or load_zendesk_config()
    requested_scope = (scope or config["scope"]).strip()
    if not requested_scope:
        raise ZendeskError("Zendesk OAuth scope cannot be empty.")

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
