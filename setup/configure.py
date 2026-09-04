#!/usr/bin/env python3
"""Interactive configuration wizard for Entra -> Zendesk Sync.

Planned flow:
1. Validate Microsoft Graph authentication.
2. Discover Entra groups and select the groups in provisioning scope.
3. Validate Zendesk OAuth authentication.
4. Discover Zendesk organizations.
5. Map selected Entra groups to Zendesk organizations.
6. Save non-secret configuration to config/config.yaml.
"""

from __future__ import annotations


def main() -> int:
    print("Entra -> Zendesk Sync configuration wizard")
    print("Configuration workflow not implemented yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
