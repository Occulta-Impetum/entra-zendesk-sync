#!/usr/bin/env python3
"""Operational Entra -> Zendesk synchronization entry point.

Normal scheduled operation is incremental: Entra is the authoritative change
source and Zendesk is queried only for identities whose Entra state changed.
Use --full-reconcile to intentionally compare every managed user to live Zendesk.
"""

from __future__ import annotations

import argparse

from lib.logging_utils import ConsoleLogTee
from lib.operational import run_incremental_dry_run
from lib.runtime import RuntimeOptions, run_read_only


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Operational Entra -> Zendesk synchronization using external-ID-only identity matching."
    )
    parser.add_argument(
        "--full-reconcile",
        action="store_true",
        help=(
            "Download a fresh complete Zendesk user snapshot and reconcile all managed users. "
            "Use this to restore Zendesk to authoritative Entra values after manual Zendesk changes."
        ),
    )
    parser.add_argument(
        "--refresh-zendesk-cache",
        action="store_true",
        help="Deprecated alias for --full-reconcile.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply operational changes. Write execution remains disabled until the incremental plan is validated.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply:
        print(
            "ERROR: operational --apply is intentionally disabled until the new incremental, "
            "email-reuse, and targeted-lookup workflow has been validated in dry-run mode. "
            "No write-capable Zendesk token was requested and no changes were made."
        )
        return 2

    full_reconcile = bool(args.full_reconcile or args.refresh_zendesk_cache)
    if full_reconcile:
        options = RuntimeOptions(
            label="OPERATIONAL FULL RECONCILE DRY RUN",
            allow_email_bootstrap=False,
            force_refresh=True,
            include_bootstrap_review=False,
        )
        prefix = "sync_full_reconcile_dry_run"
        runner = lambda: run_read_only(options)
    else:
        prefix = "sync_incremental_dry_run"
        runner = run_incremental_dry_run

    try:
        with ConsoleLogTee(prefix=prefix) as log_path:
            print(f"Log file: {log_path}")
            exit_code = runner()
            print(f"\nRun complete. Full output saved to: {log_path}")
            return exit_code
    except OSError as exc:
        print(f"ERROR: Unable to create sync log file: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
