#!/usr/bin/env python3
"""Initial Entra -> Zendesk bootstrap/migration workflow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.bootstrap_apply import run_bootstrap_apply  # noqa: E402
from lib.logging_utils import ConsoleLogTee  # noqa: E402
from lib.runtime import RuntimeOptions, run_read_only  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initial bootstrap linking of Entra users to existing Zendesk users."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--final-dry-run",
        action="store_true",
        help=(
            "Refresh Zendesk from live data and rebuild the approved bootstrap plan. "
            "This still uses initial email-bootstrap matching because it previews the first migration apply."
        ),
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Execute the one-time bootstrap migration. Re-reads live Entra and Zendesk state, "
            "refuses unresolved reviews/conflicts, requests users:read users:write, and requires "
            "an interactive APPLY confirmation before any write."
        ),
    )
    parser.add_argument(
        "--refresh-zendesk-cache",
        action="store_true",
        help="Force a fresh Zendesk user snapshot without changing bootstrap identity rules.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.apply:
        prefix = "bootstrap_apply"
        try:
            with ConsoleLogTee(prefix=prefix) as log_path:
                print(f"Log file: {log_path}")
                exit_code = run_bootstrap_apply()
                print(f"\nRun complete. Full output saved to: {log_path}")
                return exit_code
        except OSError as exc:
            print(f"ERROR: Unable to create bootstrap apply log file: {exc}")
            return 1

    final = bool(args.final_dry_run)
    options = RuntimeOptions(
        label="BOOTSTRAP FINAL DRY RUN" if final else "BOOTSTRAP DRY RUN",
        allow_email_bootstrap=True,
        force_refresh=bool(final or args.refresh_zendesk_cache),
        include_bootstrap_review=True,
    )
    prefix = "bootstrap_final_dry_run" if final else "bootstrap_dry_run"

    try:
        with ConsoleLogTee(prefix=prefix) as log_path:
            print(f"Log file: {log_path}")
            exit_code = run_read_only(options)
            print(f"\nRun complete. Full output saved to: {log_path}")
            return exit_code
    except OSError as exc:
        print(f"ERROR: Unable to create bootstrap log file: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
