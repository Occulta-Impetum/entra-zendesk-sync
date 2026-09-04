#!/usr/bin/env python3
"""Read-only Microsoft Graph authentication test.

This will be the first functional setup script. It will obtain an app-only
Graph token and perform a harmless read query so unattended authentication can
be validated before any Zendesk synchronization logic is enabled.
"""

from __future__ import annotations


def main() -> int:
    print("Microsoft Graph authentication test")
    print("Not implemented yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
