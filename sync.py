#!/usr/bin/env python3
"""Main entry point for Entra -> Zendesk synchronization.

The synchronization engine will be implemented after authentication and
configuration discovery are completed. The production command will remain
safe-by-default: dry-run unless --apply is explicitly supplied.
"""

from __future__ import annotations

import argparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize selected Microsoft Entra users to Zendesk."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag the sync will run in dry-run mode.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"Entra -> Zendesk Sync ({mode})")
    print("Sync engine not implemented yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
