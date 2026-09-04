#!/usr/bin/env python3
"""Read-only Zendesk OAuth and organization discovery test."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.zendesk import (  # noqa: E402
    ZendeskError,
    get_access_token,
    get_organizations,
    load_zendesk_config,
)


def _safe(value: object | None) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"


def main() -> int:
    print("Zendesk OAuth Authentication Test")
    print("=================================\n")

    try:
        config = load_zendesk_config()

        print(f"Zendesk subdomain: {config['subdomain']}")
        print(f"OAuth client ID:   {config['client_id']}")
        print(f"Requested scope:   {config['scope']}")

        print("\n[1/2] Acquiring Zendesk OAuth token...", flush=True)
        token, token_data = get_access_token(config)
        print("      Authentication successful.")

        granted_scope = token_data.get("scope") or token_data.get("scopes")
        expires_in = token_data.get("expires_in")
        if granted_scope:
            print(f"      Granted scope: {granted_scope}")
        if expires_in is not None:
            print(f"      Token lifetime: {expires_in} seconds")
        print("      Access token was not displayed or saved.")

        print("\n[2/2] Reading Zendesk organizations...", flush=True)
        organizations = get_organizations(token, config["subdomain"])
        print(
            f"      Organization discovery successful. "
            f"Found {len(organizations)} organization(s)."
        )

        if organizations:
            print("\nOrganizations")
            print("-------------")
            for organization in organizations:
                print(
                    f"{_safe(organization.get('name'))} | "
                    f"ID: {_safe(organization.get('id'))}"
                )

    except ZendeskError as exc:
        print(f"\nERROR: {exc}")
        return 1

    print("\nZendesk OAuth authentication and organization read access verified successfully.")
    print("No Zendesk data was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
