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
BATCH_SIZE = 20


class GraphError(RuntimeError):
    """Raised when Graph configuration, authentication, or requests fail."""


def load_graph_config() -> dict[str, str]:
    load_dotenv(override=True)
    config = {
        "tenant_id": (os.getenv("ENTRA_TENANT_ID") or "").strip(),
        "client_id": (os.getenv("ENTRA_CLIENT_ID") or "").strip(),
        "certificate_path": (os.getenv("ENTRA_CERTIFICATE_PATH") or "").strip(),
        "certificate_password": os.getenv("ENTRA_CERTIFICATE_PASSWORD") or "",
    }
    missing = [key for key in ("tenant_id", "client_id", "certificate_path") if not config[key]]
    if missing:
        raise GraphError("Missing required Microsoft Graph configuration: " + ", ".join(missing))

    cert_path = Path(config["certificate_path"]).expanduser()
    if not cert_path.is_absolute():
        cert_path = (Path.cwd() / cert_path).resolve()
    if not cert_path.is_file():
        raise GraphError(f"PFX certificate file not found: {cert_path}")
    config["certificate_path"] = str(cert_path)
    return config


def get_graph_access_token(config: dict[str, str] | None = None) -> str:
    config = config or load_graph_config()
    authority = f"https://login.microsoftonline.com/{config['tenant_id']}"
    credential: dict[str, Any] = {"private_key_pfx_path": config["certificate_path"]}
    if config.get("certificate_password"):
        credential["passphrase"] = config["certificate_password"]
    try:
        app = msal.ConfidentialClientApplication(
            client_id=config["client_id"],
            authority=authority,
            client_credential=credential,
        )
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPES)
    except Exception as exc:
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
    url = path_or_url if path_or_url.lower().startswith("https://") else f"{GRAPH_BASE_URL}/{path_or_url.lstrip('/')}"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Microsoft Graph request failed: {exc}") from exc
    if not response.ok:
        detail = " ".join((response.text or "").split())[:1000]
        raise GraphError(f"GET {response.url} failed with HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise GraphError(f"Microsoft Graph returned invalid JSON from {response.url}") from exc


def graph_post(
    path_or_url: str,
    *,
    access_token: str,
    json_body: dict[str, Any],
) -> dict[str, Any]:
    url = path_or_url if path_or_url.lower().startswith("https://") else f"{GRAPH_BASE_URL}/{path_or_url.lstrip('/')}"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Microsoft Graph request failed: {exc}") from exc
    if not response.ok:
        detail = " ".join((response.text or "").split())[:1000]
        raise GraphError(f"POST {response.url} failed with HTTP {response.status_code}: {detail}")
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
    items: list[dict[str, Any]] = []
    next_url = path_or_url
    first_request = True
    page = 0
    while next_url:
        page += 1
        print(
            f"      Fetching {progress_label} page {page} ({len(items)} received so far)...",
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
    payload = graph_get(
        "/users",
        access_token=access_token,
        params={
            "$top": str(limit),
            "$select": (
                "id,displayName,userPrincipalName,mail,accountEnabled,companyName,officeLocation,"
                "employeeId,jobTitle"
            ),
            "$orderby": "displayName",
        },
    )
    return list(payload.get("value", []))


def get_security_groups(access_token: str) -> list[dict[str, Any]]:
    groups = graph_get_all(
        "/groups",
        access_token=access_token,
        params={
            "$select": "id,displayName,description,mailEnabled,securityEnabled,groupTypes",
            "$filter": "securityEnabled eq true",
            "$top": "100",
        },
        progress_label="security groups",
    )
    return sorted(groups, key=lambda group: (group.get("displayName") or "").lower())


def get_group_user_members(access_token: str, group_id: str) -> list[dict[str, Any]]:
    """Return direct user members with their manager relationship expanded inline."""
    return graph_get_all(
        f"/groups/{group_id}/members/microsoft.graph.user",
        access_token=access_token,
        params={
            "$select": (
                "id,displayName,userPrincipalName,mail,accountEnabled,companyName,officeLocation,"
                "employeeId,jobTitle"
            ),
            "$expand": "manager($select=id,displayName,mail,userPrincipalName)",
            "$top": "100",
        },
        progress_label="group members with manager",
    )


def get_user_managers(
    access_token: str,
    user_ids: list[str] | set[str],
) -> dict[str, dict[str, Any] | None]:
    """Fallback helper: resolve managers for many users using Graph JSON batching.

    Normal group-member discovery now expands manager inline and should not need
    this helper. It is retained temporarily for compatibility and diagnostics.
    """
    ordered_ids = sorted({str(user_id).strip() for user_id in user_ids if str(user_id).strip()})
    result: dict[str, dict[str, Any] | None] = {user_id: None for user_id in ordered_ids}
    total_batches = (len(ordered_ids) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_index in range(total_batches):
        chunk = ordered_ids[batch_index * BATCH_SIZE : (batch_index + 1) * BATCH_SIZE]
        print(
            f"      Fetching managers batch {batch_index + 1}/{total_batches} "
            f"({len(chunk)} user(s))...",
            flush=True,
        )
        requests_body = []
        request_to_user: dict[str, str] = {}
        for index, user_id in enumerate(chunk, start=1):
            request_id = str(index)
            request_to_user[request_id] = user_id
            requests_body.append(
                {
                    "id": request_id,
                    "method": "GET",
                    "url": f"/users/{user_id}/manager?$select=id,displayName,mail,userPrincipalName",
                }
            )
        payload = graph_post(
            "/$batch",
            access_token=access_token,
            json_body={"requests": requests_body},
        )
        responses = payload.get("responses", [])
        if not isinstance(responses, list):
            raise GraphError("Microsoft Graph batch response did not contain a responses list.")
        for response in responses:
            request_id = str(response.get("id") or "")
            user_id = request_to_user.get(request_id)
            if not user_id:
                continue
            status = int(response.get("status") or 0)
            if status == 200 and isinstance(response.get("body"), dict):
                result[user_id] = response["body"]
            elif status == 404:
                result[user_id] = None
            else:
                body = response.get("body") or {}
                message = ""
                if isinstance(body, dict):
                    error = body.get("error") or {}
                    if isinstance(error, dict):
                        message = str(error.get("message") or "")
                raise GraphError(
                    f"Manager lookup failed for Entra user {user_id} with HTTP {status}: {message or body}"
                )
    return result
