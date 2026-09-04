#!/usr/bin/env python3
"""Read-only Entra security-group discovery test.

Authenticates using the configured application certificate, enumerates Entra
security groups, allows one group to be selected interactively, and reads its
direct user members. This script never modifies Entra.
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
    get_group_user_members,
    get_security_groups,
    load_graph_config,
)


def _safe(value: object | None) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"


def _choose_group(groups: list[dict]) -> dict | None:
    if not groups:
        return None

    print("\nSecurity groups")
    print("---------------")
    for index, group in enumerate(groups, start=1):
        name = _safe(group.get("displayName"))
        description = _safe(group.get("description"))
        print(f"{index:>3}. {name}")
        if description != "-":
            print(f"     {description}")

    while True:
        choice = input("\nSelect a group number to inspect, or Q to quit: ").strip()
        if choice.lower() == "q":
            return None
        try:
            index = int(choice)
        except ValueError:
            print("Enter a valid group number or Q.")
            continue
        if 1 <= index <= len(groups):
            return groups[index - 1]
        print(f"Enter a number from 1 to {len(groups)}.")


def main() -> int:
    print("Microsoft Entra Group Discovery Test")
    print("====================================\n")

    try:
        config = load_graph_config()

        print("[1/3] Acquiring app-only Microsoft Graph token...", flush=True)
        token = get_graph_access_token(config)
        print("      Authentication successful.")

        print("\n[2/3] Enumerating Entra security groups...", flush=True)
        groups = get_security_groups(token)
        print(f"      Discovery successful. Found {len(groups)} security group(s).")

        selected = _choose_group(groups)
        if selected is None:
            print("\nNo group selected. No Entra data was modified.")
            return 0

        print("\nSelected group")
        print("--------------")
        print(f"Name:      {_safe(selected.get('displayName'))}")
        print(f"Entra ID:  {_safe(selected.get('id'))}")

        print("\n[3/3] Reading direct user members...", flush=True)
        members = get_group_user_members(token, str(selected["id"]))
        print(f"      Membership query successful. Found {len(members)} direct user member(s).")

        if members:
            print("\nMembers")
            print("-------")
            for member in sorted(
                members, key=lambda item: (item.get("displayName") or "").lower()
            ):
                print(
                    f"{_safe(member.get('displayName'))} | "
                    f"{_safe(member.get('userPrincipalName'))} | "
                    f"Enabled: {_safe(member.get('accountEnabled'))}"
                )

    except GraphError as exc:
        print(f"\nERROR: {exc}")
        return 1

    print("\nEntra security-group discovery and membership read verified successfully.")
    print("No Entra data was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
