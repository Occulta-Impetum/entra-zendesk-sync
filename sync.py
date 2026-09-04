#!/usr/bin/env python3
"""Operational Entra -> Zendesk synchronization entry point.

This root script is intentionally small because it is the script intended for
scheduled production use. Bootstrap/migration workflows live under setup/.
"""

from __future__ import annotations

import argparse

from lib.logging_utils import ConsoleLogTee
from lib.runtime import RuntimeOptions, run_read_only


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Operational Entra -> Zendesk synchronization using external-ID-only identity matching."
    )
    parser.add_argument(
        "--refresh-zendesk-cache",
        action="store_true",
        help="Force a fresh Zendesk user snapshot for this operational dry run.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply operational changes. Write execution is not implemented yet.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply:
        print(
            "ERROR: operational --apply is intentionally disabled in this milestone. "
            "No write-capable Zendesk token was requested and no changes were made."
        )
        return 2

    options = RuntimeOptions(
        label="OPERATIONAL DRY RUN",
        allow_email_bootstrap=False,
        force_refresh=bool(args.refresh_zendesk_cache),
        include_bootstrap_review=False,
    )

    try:
        with ConsoleLogTee(prefix="sync_dry_run") as log_path:
            print(f"Log file: {log_path}")
            exit_code = run_read_only(options)
            print(f"\nRun complete. Full output saved to: {log_path}")
            return exit_code
    except OSError as exc:
        print(f"ERROR: Unable to create sync log file: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
