#!/usr/bin/env python3
"""Recover Zendesk end-users created by SSO before Entra provisioning linked them.

This tool is intentionally narrow. It only adopts current operational email
collisions where the live Zendesk user is an unclaimed end-user whose name and
email exactly match the authoritative Entra conflict snapshot and whose
external_id is blank. It never replaces a nonblank external_id.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.conflicts import ConflictSnapshotError, load_conflicts  # noqa: E402
from lib.reconcile import EXTERNAL_ID_PREFIX  # noqa: E402
from lib.zendesk import (  # noqa: E402
    ZendeskError,
    find_users_by_external_id,
    get_access_token,
    get_user,
    load_zendesk_config,
    update_user,
)

READ_SCOPE = "users:read"
WRITE_SCOPE = "users:read users:write"
TARGET_CONFLICT_TYPE = "operational_email_collision"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Review or repair unclaimed Zendesk end-users created by SSO before "
            "the Entra provisioning sync assigned external_id."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write verified Entra external IDs after an interactive ADOPT confirmation.",
    )
    return parser.parse_args()


def _norm(value: object | None) -> str:
    return str(value or "").strip()


def _norm_email(value: object | None) -> str:
    return _norm(value).lower()


def main() -> int:
    args = parse_args()
    try:
        conflicts = [
            item
            for item in load_conflicts()
            if str(item.get("conflict_type") or "") == TARGET_CONFLICT_TYPE
        ]
        if not conflicts:
            print("No operational SSO email-collision conflicts are present in cache/conflicts.json.")
            print("Run: python ./sync.py --full-reconcile")
            return 0

        config = load_zendesk_config()
        subdomain = config["subdomain"]
        read_token, _ = get_access_token(config, scope=READ_SCOPE)

        approved: list[tuple[int, str, str]] = []
        blocked = 0

        print(f"Reviewing {len(conflicts)} operational SSO adoption candidate(s)...")
        print()

        for conflict in conflicts:
            entra_id = _norm(conflict.get("entra_id"))
            expected_external = f"{EXTERNAL_ID_PREFIX}{entra_id}"
            expected_email = _norm_email(conflict.get("email"))
            expected_name = _norm(conflict.get("name"))
            zendesk_id = int(conflict["zendesk_id"])

            live = get_user(read_token, subdomain, zendesk_id)
            problems: list[str] = []

            if _norm(live.get("role")).lower() != "end-user":
                problems.append(f"live Zendesk role is {live.get('role')!r}, not end-user")
            if _norm_email(live.get("email")) != expected_email:
                problems.append(
                    f"live Zendesk email {live.get('email')!r} does not match {expected_email!r}"
                )
            if _norm(live.get("name")).casefold() != expected_name.casefold():
                problems.append(
                    f"live Zendesk name {live.get('name')!r} does not match {expected_name!r}"
                )

            live_external = _norm(live.get("external_id"))
            if live_external:
                problems.append(
                    f"live Zendesk external_id is already {live_external!r}; this tool never replaces it"
                )

            owners = find_users_by_external_id(read_token, subdomain, expected_external)
            other_owners = [
                item for item in owners if int(item.get("id") or 0) != zendesk_id
            ]
            if other_owners:
                problems.append(
                    "target external_id is already owned by Zendesk user(s): "
                    + ", ".join(str(item.get("id")) for item in other_owners)
                )

            if problems:
                blocked += 1
                print(f"BLOCKED: {expected_name} <{expected_email}> | Zendesk {zendesk_id}")
                for problem in problems:
                    print(f"    - {problem}")
                continue

            print(
                f"READY: {expected_name} <{expected_email}> | "
                f"Zendesk {zendesk_id} -> {expected_external}"
            )
            approved.append((zendesk_id, expected_name, expected_external))

        print()
        print(f"READY: {len(approved)}")
        print(f"BLOCKED: {blocked}")

        if blocked:
            print("No changes were made because at least one candidate failed validation.")
            return 1

        if not args.apply:
            print("Preview complete. No Zendesk data was modified.")
            print("To apply these reviewed adoptions, run:")
            print("  python ./setup/recover_sso_adoptions.py --apply")
            return 0

        confirmation = input(
            f"Type ADOPT to assign Entra external IDs to all {len(approved)} verified Zendesk users: "
        ).strip()
        if confirmation != "ADOPT":
            print("Cancelled. No Zendesk data was modified.")
            return 1

        write_token, _ = get_access_token(config, scope=WRITE_SCOPE)
        for index, (zendesk_id, name, external_id) in enumerate(approved, start=1):
            print(
                f"[{index}/{len(approved)}] {name}: setting external_id {external_id}...",
                flush=True,
            )
            update_user(
                write_token,
                subdomain,
                zendesk_id,
                fields={"external_id": external_id},
            )

        print("SSO adoption repair complete.")
        print("Next: python ./sync.py --full-reconcile")
        return 0

    except (ConflictSnapshotError, ZendeskError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
