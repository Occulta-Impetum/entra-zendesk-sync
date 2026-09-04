#!/usr/bin/env python3
"""Diagnostic: test whether Graph can return manager inline with group members."""

from __future__ import annotations

import argparse
from typing import Any

from lib.config import ConfigError, load_config, validate_config
from lib.graph import GraphError, get_graph_access_token, graph_get_all, load_graph_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Test whether Microsoft Graph returns each user's manager inline when reading "
            "members of a configured Entra security group."
        )
    )
    parser.add_argument(
        "--group-id",
        help="Optional Entra group object ID. Defaults to the first configured mapping.",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        help="Number of returned users to display (default: 10).",
    )
    return parser.parse_args()


def _choose_group(config: dict[str, Any], requested_group_id: str | None) -> tuple[str, str]:
    mappings = list(config.get("mappings") or [])
    if requested_group_id:
        wanted = requested_group_id.strip().lower()
        for mapping in mappings:
            group = mapping.get("entra_group") or {}
            group_id = str(group.get("id") or "").strip()
            if group_id.lower() == wanted:
                return group_id, str(group.get("name") or group_id)
        return requested_group_id.strip(), requested_group_id.strip()

    group = (mappings[0].get("entra_group") or {})
    group_id = str(group.get("id") or "").strip()
    group_name = str(group.get("name") or group_id)
    if not group_id:
        raise ConfigError("The first configured mapping is missing entra_group.id.")
    return group_id, group_name


def main() -> int:
    args = parse_args()
    print("Microsoft Graph Group-Member Manager Expansion Diagnostic")
    print("========================================================")

    try:
        print("\n[1/3] Loading configuration...", flush=True)
        config = load_config()
        validate_config(config)
        group_id, group_name = _choose_group(config, args.group_id)
        print(f"      Test group: {group_name} [{group_id}]")

        print("\n[2/3] Authenticating to Microsoft Graph...", flush=True)
        token = get_graph_access_token(load_graph_config())
        print("      Microsoft Graph authentication successful.")

        print("\n[3/3] Requesting group members with manager expanded inline...", flush=True)
        members = graph_get_all(
            f"/groups/{group_id}/members/microsoft.graph.user",
            access_token=token,
            params={
                "$select": (
                    "id,displayName,userPrincipalName,mail,accountEnabled,employeeId,jobTitle"
                ),
                "$expand": "manager($select=id,displayName,mail,userPrincipalName)",
                "$top": "100",
            },
            progress_label="group members with manager",
        )

        manager_present = 0
        manager_missing = 0
        manager_not_returned = 0
        for member in members:
            if "manager" not in member:
                manager_not_returned += 1
            elif isinstance(member.get("manager"), dict):
                manager_present += 1
            else:
                manager_missing += 1

        print("\nResult")
        print("------")
        print(f"Users returned:                  {len(members)}")
        print(f"Manager object returned:         {manager_present}")
        print(f"Manager explicitly empty/null:   {manager_missing}")
        print(f"Manager property not returned:   {manager_not_returned}")

        print("\nSample users")
        print("------------")
        show = max(0, args.show)
        for member in members[:show]:
            email = str(member.get("mail") or member.get("userPrincipalName") or "-")
            manager = member.get("manager")
            print(f"{member.get('displayName') or '-'} <{email}>")
            if isinstance(manager, dict):
                manager_email = str(manager.get("mail") or manager.get("userPrincipalName") or "-")
                print(f"  Manager: {manager.get('displayName') or '-'} <{manager_email}>")
                print(f"  Manager ID: {manager.get('id') or '-'}")
            elif "manager" in member:
                print("  Manager: <none/null>")
            else:
                print("  Manager: <property was not returned by Graph>")

        print("\nInterpretation")
        print("--------------")
        if manager_present > 0 and manager_not_returned == 0:
            print(
                "SUCCESS: Graph is returning manager inline with group members. "
                "The operational sync can likely remove the separate 20-user manager batch pass."
            )
            return 0
        if manager_present > 0:
            print(
                "PARTIAL SUCCESS: Graph returned manager for some users but omitted the property for others. "
                "Review the sample/output before changing production behavior."
            )
            return 1

        print(
            "NOT CONFIRMED: Graph did not return any manager objects inline. "
            "Keep the current separate manager lookup until we understand the response."
        )
        return 1

    except (ConfigError, GraphError, KeyError, ValueError) as exc:
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
