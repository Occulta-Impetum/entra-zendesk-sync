#!/usr/bin/env python3
"""Inspect Zendesk user fields and optionally verify a Manager lookup on one user."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.zendesk import (  # noqa: E402
    ZendeskError,
    get_access_token,
    load_zendesk_config,
    zendesk_get,
)

READ_SCOPE = "users:read account_settings:read"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect Zendesk custom user fields and optionally verify how a Manager "
            "lookup field stores its relationship to another Zendesk user."
        )
    )
    parser.add_argument(
        "--email",
        help=(
            "Optional Zendesk user email to inspect. If omitted, the script only "
            "reports user-field definitions and Manager lookup metadata."
        ),
    )
    return parser.parse_args()


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _list_user_fields(*, access_token: str, subdomain: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    next_url = "user_fields.json"
    page = 0
    params: dict[str, str] | None = {"per_page": "100"}

    while next_url:
        page += 1
        print(
            f"      Fetching Zendesk user fields page {page} "
            f"({len(fields)} received so far)...",
            flush=True,
        )
        payload = zendesk_get(
            next_url,
            subdomain=subdomain,
            access_token=access_token,
            params=params,
        )
        params = None
        page_items = payload.get("user_fields", [])
        if not isinstance(page_items, list):
            raise ZendeskError("Zendesk user-fields response did not contain a list.")
        fields.extend(page_items)
        next_url = str(payload.get("next_page") or "")

    return fields


def _search_exact_user(
    email: str,
    *,
    access_token: str,
    subdomain: str,
) -> dict[str, Any]:
    print(f"      Searching Zendesk for exact email: {email}", flush=True)
    payload = zendesk_get(
        "search.json",
        subdomain=subdomain,
        access_token=access_token,
        params={"query": f"type:user email:{email}"},
    )
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ZendeskError("Zendesk search response did not contain a results list.")

    exact = [
        item
        for item in results
        if str(item.get("result_type") or "") == "user"
        and _norm(item.get("email")) == _norm(email)
    ]
    if len(exact) != 1:
        raise ZendeskError(
            f"Expected one exact Zendesk user for {email}, found {len(exact)}."
        )

    user_id = exact[0].get("id")
    if user_id is None:
        raise ZendeskError(f"Zendesk search result for {email} did not include a user id.")

    payload = zendesk_get(
        f"users/{int(user_id)}.json",
        subdomain=subdomain,
        access_token=access_token,
    )
    user = payload.get("user")
    if not isinstance(user, dict):
        raise ZendeskError(f"Zendesk user {user_id} response did not contain a user object.")
    return user


def _find_relevant_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terms = ("manager", "title", "employee", "employee_id", "employee id")
    relevant: list[dict[str, Any]] = []
    for field in fields:
        haystack = " ".join(
            [
                _norm(field.get("title")),
                _norm(field.get("raw_title")),
                _norm(field.get("key")),
            ]
        )
        if any(term in haystack for term in terms):
            relevant.append(field)
    return sorted(relevant, key=lambda item: (_norm(item.get("title")), _norm(item.get("key"))))


def _manager_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        field
        for field in fields
        if "manager" in f"{_norm(field.get('title'))} {_norm(field.get('key'))}"
    ]


def _print_field(field: dict[str, Any]) -> None:
    print(f"Title: {field.get('title') or '-'}")
    print(f"  Key: {field.get('key') or '-'}")
    print(f"  ID: {field.get('id') or '-'}")
    print(f"  Type: {field.get('type') or '-'}")
    print(f"  Active: {field.get('active')}")
    if field.get("relationship_target_type"):
        print(f"  Relationship target: {field.get('relationship_target_type')}")
    if field.get("relationship_filter"):
        print(f"  Relationship filter: {field.get('relationship_filter')}")
    print()


def _resolve_lookup_target(
    raw_value: object,
    *,
    access_token: str,
    subdomain: str,
) -> dict[str, Any] | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        target_id = int(value)
    except ValueError:
        return None

    payload = zendesk_get(
        f"users/{target_id}.json",
        subdomain=subdomain,
        access_token=access_token,
    )
    user = payload.get("user")
    return user if isinstance(user, dict) else None


def main() -> int:
    args = parse_args()
    email = str(args.email or "").strip()

    try:
        print("Zendesk User Field / Manager Lookup Check")
        print("========================================")

        print("\n[1] Authenticating to Zendesk with required read-only scopes...", flush=True)
        config = load_zendesk_config()
        print(f"      Requested scope: {READ_SCOPE}")
        token, token_data = get_access_token(config, scope=READ_SCOPE)
        granted = token_data.get("scope") or token_data.get("scopes") or "not reported"
        print(f"      Zendesk authentication successful. Granted scope: {granted}")

        print("\n[2] Reading Zendesk custom user-field definitions...", flush=True)
        fields = _list_user_fields(access_token=token, subdomain=config["subdomain"])
        relevant = _find_relevant_fields(fields)
        print(f"      {len(fields)} total custom user field(s) found.")
        print(f"      {len(relevant)} field(s) look relevant to Employee ID, Title, or Manager.")

        if relevant:
            print("\nRelevant Zendesk user fields")
            print("----------------------------")
            for field in relevant:
                _print_field(field)
        else:
            print("      No likely Employee ID, Title, or Manager fields were found by title/key.")

        managers = _manager_fields(fields)
        if managers:
            print("Manager field schema")
            print("--------------------")
            for field in managers:
                _print_field(field)
        else:
            print("No Zendesk user field with 'manager' in its title/key was found.")

        if not email:
            print(
                "No --email was supplied, so no individual Zendesk user was inspected.\n"
                "To verify a populated Manager relationship, rerun with:\n"
                "  python .\\setup\\check_user_fields.py --email user@example.com"
            )
            return 0

        print(f"[3] Inspecting {email}...", flush=True)
        user = _search_exact_user(
            email,
            access_token=token,
            subdomain=config["subdomain"],
        )
        print(f"      Zendesk user ID: {user.get('id')}")
        print(f"      Name: {user.get('name') or '-'}")
        print(f"      Email: {user.get('email') or '-'}")

        user_fields = user.get("user_fields") or {}
        if not isinstance(user_fields, dict):
            user_fields = {}

        print("\n[4] Checking Manager lookup relationship...", flush=True)
        if not managers:
            print("      No Manager field was available to inspect.")
            return 1

        found_lookup = False
        for field in managers:
            key = str(field.get("key") or "")
            raw_value = user_fields.get(key)
            print(f"      Field: {field.get('title') or key} [{key}]")
            print(f"      Type: {field.get('type') or '-'}")
            print(f"      Relationship target: {field.get('relationship_target_type') or '-'}")
            print(f"      Stored value on {email}: {raw_value!r}")

            if field.get("type") == "lookup" and field.get("relationship_target_type") == "zen:user":
                found_lookup = True
                target = _resolve_lookup_target(
                    raw_value,
                    access_token=token,
                    subdomain=config["subdomain"],
                )
                if target:
                    print(
                        "      Resolved target: "
                        f"{target.get('name') or '-'} <{target.get('email') or '-'}> "
                        f"[Zendesk user ID: {target.get('id')}]"
                    )
                    print(
                        "      Result: this Manager field is a Zendesk user lookup relationship. "
                        "The stored value resolves to the target Zendesk user."
                    )
                elif raw_value in (None, ""):
                    print("      Manager is currently blank on this user.")
                else:
                    print(
                        "      The field is a user lookup, but the stored value could not be "
                        "resolved as a numeric Zendesk user ID."
                    )
            print()

        if found_lookup:
            print("Recommended sync behavior")
            print("-------------------------")
            print(
                "Manager should be synchronized as a Zendesk user lookup relationship, not as text. "
                "Bootstrap manager relationships should be written in a second pass after all users "
                "have been created/adopted so every manager target exists first."
            )
            return 0

        print(
            "No Manager field was confirmed as type=lookup with relationship_target_type=zen:user."
        )
        return 1

    except ZendeskError as exc:
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
