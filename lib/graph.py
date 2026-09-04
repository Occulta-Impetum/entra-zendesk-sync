"""Microsoft Graph authentication and discovery helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import msal
import requests
from dotenv import load_dotenv

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
REQUEST_TIMEOUT = 30


class GraphError(RuntimeError):
    """Raised when Graph configuration, authentication, or requests fail."""


def load_graph_config() -> dict[str, str]:
    """Load machine-specific Graph settings from .env.

    PFX certificate authentication is used so the same configuration model can
    be used for local testing and unattended scheduled execution.
    """
    load_dotenv(override=True)

    config = {
        "tenant_id": (os.getenv("ENTRA_TENANT_ID") or "").strip(),
        "client_id": (os.getenv("ENTRA_CLIENT_ID") or "").strip(),
        "certificate_path": (os.getenv("ENTRA_CERTIFICATE_PATH") or "").strip(),
        "certificate_password": os.getenv("ENTRA_CERTIFICATE_PASSWORD") or "",
    }

    missing = [
        key
        for key in ("tenant_id", "client_id", "certificate_path")
        if not config[key]
    ]
    if missing:
        raise GraphError(
            "Missing required Microsoft Graph configuration: " + ", ".join(missing)
        )

    cert_path = Path(config["certificate_path"]).expanduser()
    if not cert_path.is_absolute():
        cert_path = (Path.cwd() / cert_path).resolve()

    if not cert_path.is_file():
        raise GraphError(f"PFX certificate file not found: {cert_path}")

    config["certificate_path"] = str(cert_path)
    return config


def get_graph_access_token(config: dict[str, str] | None = None) -> str:
    """Acquire an app-only Microsoft Graph access token using a PFX certificate."""
    config = config or load_graph_config()

    authority = f"https://login.microsoftonline.com/{config['tenant_id']}"
    credential: dict[str, Any] = {
        "private_key_pfx_path": config["certificate_path"],
    }
    if config.get("certificate_password"):
        credential["passphrase"] = config["certificate_password"]

    try:
        app = msal.ConfidentialClientApplication(
            client_id=config["client_id"],
            authority=authority,
            client_credential=credential,
        )
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
    except Exception as exc:  # MSAL/PFX parsing errors vary by dependency/version.
        raise GraphError(f"Unable to initialize certificate authentication: {exc}") from exc

    token = result.get("access_token")
    if token:
        return str(token)

    error = result.get("error", "unknown_error")
    description = result.get("error_description", "No error description returned.")
    correlation_id = result.get("correlation_id")
    detail = f"Microsoft Graph authentication failed: {error}: {description}"
    if correlation_id:
        detail += f" Correlation ID: {correlation_id}"
    raise GraphError(detail)


def graph_get(
    path_or_url: str,
    *,
    access_token: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform an authenticated GET request against Microsoft Graph."""
    url = (
        path_or_url
        if path_or_url.lower().startswith("https://")
        else f"{GRAPH_BASE_URL}/{path_or_url.lstrip('/')}"
    )

    try:
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Microsoft Graph request failed: {exc}") from exc

    if not response.ok:
        detail = " ".join((response.text or "").split())[:1000]
        raise GraphError(
            f"GET {response.url} failed with HTTP {response.status_code}: {detail}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise GraphError(f"Microsoft Graph returned invalid JSON from {response.url}") from exc


def graph_get_all(
    path_or_url: str,
    *,
    access_token: str,
    params: dict[str, str] | None = None,
    progress_label: str = "items",
) -> list[dict[str, Any]]:
    """Return all pages from a Graph collection while printing visible progress."""
    items: list[dict[str, Any]] = []
    next_url = path_or_url
    first_request = True
    page = 0

    while next_url:
        page += 1
        print(
            f"      Fetching {progress_label} page {page} "
            f"({len(items)} received so far)...",
            flush=True,
        )
        payload = graph_get(
            next_url,
            access_token=access_token,
            params=params if first_request else None,
        )
        first_request = False
        page_items = payload.get("value", [])
        if not isinstance(page_items, list):
            raise GraphError("Microsoft Graph collection response did not contain a list.")
        items.extend(page_items)
        next_url = payload.get("@odata.nextLink") or ""

    return items


def get_sample_users(access_token: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return a small read-only sample used to validate User.Read.All access."""
    payload = graph_get(
        "/users",
        access_token=access_token,
        params={
            "$top": str(limit),
            "$select": (
                "id,displayName,userPrincipalName,mail,accountEnabled,"
                "companyName,officeLocation"
            ),
            "$orderby": "displayName",
        },
    )
    return list(payload.get("value", []))


def get_security_groups(access_token: str) -> list[dict[str, Any]]:
    """Return Entra security groups, sorted by display name.

    This uses only basic group properties needed by the setup workflow.
    """
    groups = graph_get_all(
        "/groups",
        access_token=access_token,
        params={
            "$select": "id,displayName,description,mailEnabled,securityEnabled,groupTypes",
            "$filter": "securityEnabled eq true",
            "$orderby": "displayName",
            "$top": "100",
        },
        progress_label="security groups",
    )
    return sorted(groups, key=lambda group: (group.get("displayName") or "").lower())


def get_group_user_members(
    access_token: str,
    group_id: str,
) -> list[dict[str, Any]]:
    """Return direct user members of one group.

    User.Read.All supplies the user properties while the group-membership
    permission authorizes reading the membership relationship.
    """
    return graph_get_all(
        f"/groups/{group_id}/members/microsoft.graph.user",
        access_token=access_token,
        params={
            "$select": (
                "id,displayName,userPrincipalName,mail,accountEnabled,"
                "companyName,officeLocation"
            ),
            "$top": "100",
        },
        progress_label="group members",
    )
