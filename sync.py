#!/usr/bin/env python3
"""Main entry point for Entra -> Zendesk synchronization.

Safe by default: running without ``--apply`` performs a read-only reconciliation
and prints the changes that would be made. Write execution is intentionally not
implemented yet.
"""

from __future__ import annotations

import argparse
from collections import Counter

from lib.config import ConfigError, load_config, validate_config
from lib.graph import (
    GraphError,
    get_graph_access_token,
    get_group_user_members,
    load_graph_config,
)
from lib.reconcile import build_desired_users, plan_reconciliation, summarize_plan
from lib.zendesk import ZendeskError, get_access_token, get_users, load_zendesk_config

# Runtime scopes are explicit. ZENDESK_OAUTH_SCOPE remains only a default for
# callers that do not pass a scope (for example, standalone setup tests).
ZENDESK_DRY_RUN_SCOPE = "users:read"
ZENDESK_APPLY_SCOPE = "users:read users:write"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize selected Microsoft Entra users to Zendesk."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply changes. Write execution is not implemented yet; this flag "
            "is reserved for the next milestone."
        ),
    )
    return parser.parse_args()


def _behavior(config: dict, name: str, default: bool) -> bool:
    behavior = config.get("behavior") or {}
    value = behavior.get(name, default)
    return bool(value)


def _print_summary(counts: Counter[str], total_rows: int) -> None:
    print("\nReconciliation summary")
    print("----------------------")
    preferred_order = [
        "CREATE",
        "ADOPT",
        "UPDATE EMAIL",
        "UPDATE NAME",
        "UPDATE ORGANIZATION",
        "UNSUSPEND",
        "SUSPEND",
        "PROTECTED",
        "CONFLICT",
        "NO CHANGE",
    ]
    shown: set[str] = set()
    for action in preferred_order:
        if counts.get(action):
            print(f"{action:<22} {counts[action]:>5}")
            shown.add(action)
    for action in sorted(set(counts) - shown):
        print(f"{action:<22} {counts[action]:>5}")
    print(f"{'PLAN ROWS':<22} {total_rows:>5}")


def _print_details(rows: list[dict]) -> None:
    print("\nDetailed plan")
    print("-------------")
    for row in rows:
        action = str(row.get("action") or "")
        name = str(row.get("name") or "-")
        email = str(row.get("email") or "-")
        print(f"[{action}] {name} <{email}>")

        group = str(row.get("group_name") or "")
        org = str(row.get("zendesk_org_name") or "")
        matched_by = str(row.get("matched_by") or "")
        zendesk_id = row.get("zendesk_id")
        reason = str(row.get("reason") or "")
        groups = row.get("groups") or []

        if group:
            print(f"  Entra group: {group}")
        if org:
            print(f"  Desired Zendesk org: {org} [ID: {row.get('zendesk_org_id')}]")
        if zendesk_id:
            match_text = f" via {matched_by}" if matched_by else ""
            print(f"  Zendesk user: {zendesk_id}{match_text}")
        if groups:
            print("  Conflicting groups: " + ", ".join(str(item) for item in groups))
        if reason:
            print(f"  Reason: {reason}")
        print()


def main() -> int:
    args = parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    print(f"Entra -> Zendesk Sync ({mode})")
    print("=" * (24 + len(mode)))

    if args.apply:
        print(
            "\nERROR: --apply is intentionally disabled in this milestone. "
            "No write-capable Zendesk token was requested and no changes were made."
        )
        return 2

    try:
        print("\n[1/6] Loading configuration...", flush=True)
        config = load_config()
        validate_config(config)
        mappings = list(config["mappings"])
        print(f"      Configuration valid. {len(mappings)} group mapping(s) loaded.")

        print("\n[2/6] Authenticating to Microsoft Graph...", flush=True)
        graph_config = load_graph_config()
        graph_token = get_graph_access_token(graph_config)
        print("      Microsoft Graph authentication successful.")

        print("\n[3/6] Reading configured Entra group memberships...", flush=True)
        group_members: list[tuple[dict, list[dict]]] = []
        for index, mapping in enumerate(mappings, start=1):
            entra_group = mapping.get("entra_group") or {}
            group_id = str(entra_group.get("id") or "")
            group_name = str(entra_group.get("name") or group_id)
            print(
                f"      [{index}/{len(mappings)}] {group_name}: reading direct user members...",
                flush=True,
            )
            members = get_group_user_members(graph_token, group_id)
            print(f"            {len(members)} user member(s) found.")
            group_members.append((mapping, members))

        desired_users, membership_conflicts, in_scope_ids = build_desired_users(group_members)
        print(
            f"      {len(in_scope_ids)} unique Entra user(s) in scope; "
            f"{len(membership_conflicts)} multiple-group conflict(s)."
        )

        print("\n[4/6] Authenticating to Zendesk with read-only scope...", flush=True)
        zendesk_config = load_zendesk_config()
        print(f"      Requested scope: {ZENDESK_DRY_RUN_SCOPE}")
        zendesk_token, token_data = get_access_token(
            zendesk_config,
            scope=ZENDESK_DRY_RUN_SCOPE,
        )
        granted_scope = token_data.get("scope") or token_data.get("scopes") or "not reported"
        print(f"      Zendesk authentication successful. Granted scope: {granted_scope}")

        print("\n[5/6] Reading Zendesk users...", flush=True)
        zendesk_users = get_users(zendesk_token, zendesk_config["subdomain"])
        print(f"      {len(zendesk_users)} Zendesk user(s) loaded.")

        print("\n[6/6] Building reconciliation plan...", flush=True)
        plan = plan_reconciliation(
            desired_users,
            zendesk_users,
            in_scope_entra_ids=in_scope_ids,
            suspend_when_out_of_scope=_behavior(config, "suspend_when_out_of_scope", True),
            suspend_when_entra_disabled=_behavior(config, "suspend_when_entra_disabled", True),
            protect_zendesk_staff_roles=_behavior(config, "protect_zendesk_staff_roles", True),
        )
        plan.extend(membership_conflicts)
        plan.sort(key=lambda row: (str(row.get("action")), str(row.get("name") or "").lower()))
        counts = summarize_plan(plan)
        print("      Reconciliation plan complete.")

    except (ConfigError, GraphError, ZendeskError, KeyError, ValueError) as exc:
        print(f"\nERROR: {exc}")
        return 1

    _print_summary(counts, len(plan))
    _print_details(plan)

    print("DRY RUN COMPLETE: no Zendesk data was modified.")
    print(f"Only the explicit read-only Zendesk scope '{ZENDESK_DRY_RUN_SCOPE}' was requested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
