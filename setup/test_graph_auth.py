#!/usr/bin/env python3
"""Read-only Microsoft Graph authentication test.

Acquires an app-only Microsoft Graph token using the configured PFX certificate
and performs a harmless sample user query. This script never modifies Entra.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.graph import (  # noqa: E402
    GraphError,
    get_graph_access_token,
    get_sample_users,
    load_graph_config,
)


def _safe(value: object | None) -> str:
    text = str(value or "").strip()
    return text if text else "-"


def main() -> int:
    print("Microsoft Graph Authentication Test")
    print("===================================\n")

    try:
        config = load_graph_config()

        print(f"Tenant ID: {config['tenant_id']}")
        print(f"Client ID: {config['client_id']}")
        print(f"Certificate: {config['certificate_path']}")
        print("Authentication mode: PFX certificate / client credentials")
        print("Graph scope: https://graph.microsoft.com/.default")

        print("\n[1/2] Acquiring app-only Microsoft Graph token...", flush=True)
        token = get_graph_access_token(config)
        print("      Authentication successful.")

        print("\n[2/2] Testing User.Read.All with a read-only user query...", flush=True)
        users = get_sample_users(token, limit=5)
        print(f"      Query successful. Returned {len(users)} sample user(s).")

        if users:
            print("\nSample users")
            print("------------")
            for user in users:
                print(f"Name:          {_safe(user.get('displayName'))}")
                print(f"UPN:           {_safe(user.get('userPrincipalName'))}")
                print(f"Mail:          {_safe(user.get('mail'))}")
                print(f"Enabled:       {_safe(user.get('accountEnabled'))}")
                print(f"Company:       {_safe(user.get('companyName'))}")
                print(f"Office:        {_safe(user.get('officeLocation'))}")
                print(f"Entra ID:      {_safe(user.get('id'))}")
                print()

    except GraphError as exc:
        print(f"\nERROR: {exc}")
        return 1

    print("Microsoft Graph authentication and read access verified successfully.")
    print("No Entra data was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
