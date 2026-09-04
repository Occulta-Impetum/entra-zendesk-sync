"""Zendesk OAuth and API helpers."""

from __future__ import annotations

import json
import os
import tempfile
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
_TOKEN_CONTEXT: dict[str, tuple[dict[str, str], str]] = {}
_TOKEN_REPLACEMENTS: dict[str, str] = {}


class ZendeskError(RuntimeError):
    """Raised when Zendesk configuration, authentication, or requests fail."""


def load_zendesk_config() -> dict[str, str]:
    load_dotenv(override=True)
    config = {
        "subdomain": (os.getenv("ZENDESK_SUBDOMAIN") or "").strip(),
        "client_id": (os.getenv("ZENDESK_OAUTH_CLIENT_ID") or "").strip(),
        "client_secret": (os.getenv("ZENDESK_OAUTH_CLIENT_SECRET") or "").strip(),
        "scope": (os.getenv("ZENDESK_OAUTH_SCOPE") or DEFAULT_SCOPE).strip(),
    }
    missing = [name for name, value in (
        ("ZENDESK_SUBDOMAIN", config["subdomain"]),
        ("ZENDESK_OAUTH_CLIENT_ID", config["client_id"]),
        ("ZENDESK_OAUTH_CLIENT_SECRET", config["client_secret"]),
    ) if not value]
    if missing:
        raise ZendeskError("Zendesk configuration is incomplete. Missing: " + ", ".join(missing))
    subdomain = config["subdomain"].removeprefix("https://").removeprefix("http://")
    subdomain = subdomain.split(".", 1)[0].strip("/")
    if not subdomain:
        raise ZendeskError("ZENDESK_SUBDOMAIN is empty after normalization.")
    config["subdomain"] = subdomain
    return config


def _safe_error_detail(response: requests.Response, secret: str = "") -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            candidates = (body.get("error_description"), body.get("description"), body.get("message"), body.get("error"))
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
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=TOKEN_CACHE_PATH.parent,
            prefix=f".{TOKEN_CACHE_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(cache, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, TOKEN_CACHE_PATH)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _register_token(token: str, config: dict[str, str], scope_key: str) -> None:
    _TOKEN_CONTEXT[token] = (dict(config), scope_key)


def _effective_token(token: str) -> str:
    seen: set[str] = set()
    current = token
    while current in _TOKEN_REPLACEMENTS and current not in seen:
        seen.add(current)
        current = _TOKEN_REPLACEMENTS[current]
    return current


def get_access_token(config: dict[str, str] | None = None, *, scope: str | None = None, force_new: bool = False) -> tuple[str, dict[str, Any]]:
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
            _register_token(token, config, scope_key)
            print(f"      Reusing cached Zendesk OAuth token for exact scopes [{scope_key}] (~{remaining} min remaining).")
            return token, {"access_token": token, "scope": scope_key, "expires_in": max(0, int(expires_at - now)), "cached": True}
    print(f"      Requesting a new Zendesk OAuth token for exact scopes [{scope_key}]...")
    url = f"https://{config['subdomain']}.zendesk.com/oauth/tokens"
    payload = {"grant_type": "client_credentials", "client_id": config["client_id"], "client_secret": config["client_secret"], "scope": requested_scope}
    try:
        response = requests.post(url, json=payload, headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise ZendeskError(f"Could not contact Zendesk OAuth endpoint: {exc}") from exc
    if not response.ok:
        detail = _safe_error_detail(response, config["client_secret"])
        raise ZendeskError(f"Zendesk OAuth request failed with HTTP {response.status_code}." + (f" Response: {detail}" if detail else ""))
    try:
        data = response.json()
    except ValueError as exc:
        raise ZendeskError("Zendesk returned an invalid OAuth response.") from exc
    token = data.get("access_token")
    if not token:
        raise ZendeskError("Zendesk OAuth response did not contain an access token.")
    token = str(token)
    expires_in = int(data.get("expires_in") or 1800)
    cache[cache_key] = {"access_token": token, "scope": scope_key, "expires_at": now + expires_in}
    try:
        _save_token_cache(cache)
    except OSError:
        pass
    _register_token(token, config, scope_key)
    print(f"      Cached new Zendesk OAuth token for exact scopes [{scope_key}] (expires in ~{max(0, int(expires_in / 60) - 1)} min).")
    return token, data


def _request_once(
    method: str,
    url: str,
    *,
    access_token: str,
    params: dict[str, str] | None,
    json_body: dict[str, Any] | None,
) -> requests.Response:
    return requests.request(
        method.upper(),
        url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json"},
        params=params,
        json=json_body,
        timeout=REQUEST_TIMEOUT,
    )


def zendesk_request(method: str, path_or_url: str, *, subdomain: str, access_token: str, params: dict[str, str] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = path_or_url if path_or_url.lower().startswith("https://") else f"https://{subdomain}.zendesk.com/api/v2/{path_or_url.lstrip('/')}"
    token = _effective_token(access_token)
    try:
        response = _request_once(method, url, access_token=token, params=params, json_body=json_body)
    except requests.RequestException as exc:
        raise ZendeskError(f"Zendesk API request failed: {exc}") from exc

    if response.status_code == 401:
        context = _TOKEN_CONTEXT.get(token) or _TOKEN_CONTEXT.get(access_token)
        if context is not None:
            config, scope_key = context
            print(f"      Zendesk returned HTTP 401; refreshing exact scopes [{scope_key}] and retrying once...", flush=True)
            replacement, _ = get_access_token(config, scope=scope_key, force_new=True)
            _TOKEN_REPLACEMENTS[access_token] = replacement
            _TOKEN_REPLACEMENTS[token] = replacement
            try:
                response = _request_once(method, url, access_token=replacement, params=params, json_body=json_body)
            except requests.RequestException as exc:
                raise ZendeskError(f"Zendesk API retry failed: {exc}") from exc

    if not response.ok:
        detail = _safe_error_detail(response)
        raise ZendeskError(f"{method.upper()} {response.url} failed with HTTP {response.status_code}." + (f" Response: {detail}" if detail else ""))
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise ZendeskError(f"Zendesk returned invalid JSON from {response.url}") from exc


def zendesk_get(path_or_url: str, *, subdomain: str, access_token: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    return zendesk_request("GET", path_or_url, subdomain=subdomain, access_token=access_token, params=params)


def search_users(access_token: str, subdomain: str, query: str) -> list[dict[str, Any]]:
    """Search only Zendesk users, avoiding the broad account Search API."""
    payload = zendesk_get("users/search.json", subdomain=subdomain, access_token=access_token, params={"query": query})
    users = payload.get("users", [])
    if not isinstance(users, list):
        raise ZendeskError("Zendesk user search response did not contain a users list.")
    return users


def find_users_by_email(access_token: str, subdomain: str, email: str) -> list[dict[str, Any]]:
    wanted = str(email or "").strip().lower()
    if not wanted:
        return []
    return [user for user in search_users(access_token, subdomain, f"email:{wanted}") if str(user.get("email") or "").strip().lower() == wanted]


def find_users_by_external_id(access_token: str, subdomain: str, external_id: str) -> list[dict[str, Any]]:
    wanted = str(external_id or "").strip().lower()
    if not wanted:
        return []
    return [user for user in search_users(access_token, subdomain, f"external_id:{external_id}") if str(user.get("external_id") or "").strip().lower() == wanted]


def get_user(access_token: str, subdomain: str, user_id: int) -> dict[str, Any]:
    payload = zendesk_get(f"users/{int(user_id)}.json", subdomain=subdomain, access_token=access_token)
    user = payload.get("user")
    if not isinstance(user, dict):
        raise ZendeskError(f"Zendesk user {user_id} response did not contain a user object.")
    return user


def get_user_identities(access_token: str, subdomain: str, user_id: int) -> list[dict[str, Any]]:
    payload = zendesk_get(f"users/{int(user_id)}/identities.json", subdomain=subdomain, access_token=access_token)
    identities = payload.get("identities", [])
    if not isinstance(identities, list):
        raise ZendeskError(f"Zendesk identities response for user {user_id} did not contain a list.")
    return identities


def rename_primary_email_identity(access_token: str, subdomain: str, user_id: int, new_email: str) -> dict[str, Any]:
    """Rename the existing primary email identity while preserving the Zendesk user/tickets."""
    identities = get_user_identities(access_token, subdomain, user_id)
    primaries = [i for i in identities if i.get("type") == "email" and bool(i.get("primary"))]
    if len(primaries) != 1 or primaries[0].get("id") is None:
        raise ZendeskError(f"Expected exactly one primary email identity for Zendesk user {user_id}; found {len(primaries)}.")
    payload = zendesk_request("PUT", f"users/{int(user_id)}/identities/{int(primaries[0]['id'])}.json", subdomain=subdomain, access_token=access_token, json_body={"identity": {"value": new_email}})
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        raise ZendeskError(f"Zendesk identity update for user {user_id} did not return an identity object.")
    return identity


def get_user_fields(access_token: str, subdomain: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    next_url = "user_fields.json"
    page = 0
    params: dict[str, str] | None = {"per_page": "100"}
    while next_url:
        page += 1
        print(f"      Fetching Zendesk user fields page {page} ({len(fields)} received so far)...", flush=True)
        payload = zendesk_get(next_url, subdomain=subdomain, access_token=access_token, params=params)
        params = None
        page_items = payload.get("user_fields", [])
        if not isinstance(page_items, list):
            raise ZendeskError("Zendesk user-fields response did not contain a list.")
        fields.extend(page_items)
        next_url = str(payload.get("next_page") or "")
    return fields


def create_user_field(access_token: str, subdomain: str, *, title: str, key: str, field_type: str = "text") -> dict[str, Any]:
    data = zendesk_request("POST", "user_fields.json", subdomain=subdomain, access_token=access_token, json_body={"user_field": {"title": title, "key": key, "type": field_type, "active": True}})
    field = data.get("user_field")
    if not isinstance(field, dict):
        raise ZendeskError("Zendesk create-user-field response did not contain a user_field object.")
    return field


def create_user(access_token: str, subdomain: str, *, name: str, email: str, external_id: str, organization_id: int, user_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    user_payload: dict[str, Any] = {"name": name, "email": email, "external_id": external_id, "organization_id": organization_id, "role": "end-user"}
    if user_fields:
        user_payload["user_fields"] = user_fields
    data = zendesk_request("POST", "users.json", subdomain=subdomain, access_token=access_token, json_body={"user": user_payload})
    user = data.get("user")
    if not isinstance(user, dict) or user.get("id") is None:
        raise ZendeskError("Zendesk create-user response did not contain a user id.")
    return user


def update_user(access_token: str, subdomain: str, user_id: int, *, fields: dict[str, Any]) -> dict[str, Any]:
    if not fields:
        return {}
    data = zendesk_request("PUT", f"users/{int(user_id)}.json", subdomain=subdomain, access_token=access_token, json_body={"user": fields})
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
        print(f"      Fetching organizations page {page} ({len(organizations)} received so far)...", flush=True)
        payload = zendesk_get(next_url, subdomain=subdomain, access_token=access_token, params=params)
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
        print(f"      Fetching Zendesk users page {page} ({len(users)} received so far)...", flush=True)
        payload = zendesk_get(next_url, subdomain=subdomain, access_token=access_token, params=params)
        params = None
        page_items = payload.get("users", [])
        if not isinstance(page_items, list):
            raise ZendeskError("Zendesk users response did not contain a list.")
        users.extend(page_items)
        next_url = payload.get("next_page") or ""
    return users
