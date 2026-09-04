"""Read-only reconciliation and synchronization planning helpers."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

EXTERNAL_ID_PREFIX = "entra:"
STAFF_ROLES = {"agent", "admin"}


def _norm_email(value: object | None) -> str:
    return str(value or "").strip().lower()


def _entra_email(user: dict[str, Any]) -> str:
    return _norm_email(user.get("mail") or user.get("userPrincipalName"))


def build_desired_users(
    group_members: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Build desired user state and identify multiple-mapped-group conflicts.

    ``group_members`` contains ``(mapping, members)`` tuples where each mapping
    is one configured Entra-group -> Zendesk-organization mapping.
    """
    memberships: dict[str, list[dict[str, Any]]] = defaultdict(list)
    users_by_id: dict[str, dict[str, Any]] = {}

    for mapping, members in group_members:
        for user in members:
            user_id = str(user.get("id") or "").strip()
            if not user_id:
                continue
            users_by_id[user_id] = user
            memberships[user_id].append(mapping)

    desired: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []

    for user_id, mappings in memberships.items():
        user = users_by_id[user_id]
        if len(mappings) != 1:
            conflicts.append(
                {
                    "action": "CONFLICT",
                    "entra_id": user_id,
                    "name": str(user.get("displayName") or ""),
                    "email": _entra_email(user),
                    "reason": "User belongs to multiple mapped Entra groups.",
                    "groups": [
                        str((mapping.get("entra_group") or {}).get("name") or "")
                        for mapping in mappings
                    ],
                }
            )
            continue

        mapping = mappings[0]
        zendesk_org = mapping.get("zendesk_organization") or {}
        desired[user_id] = {
            "entra_id": user_id,
            "name": str(user.get("displayName") or "").strip(),
            "email": _entra_email(user),
            "enabled": bool(user.get("accountEnabled")),
            "group_name": str((mapping.get("entra_group") or {}).get("name") or ""),
            "zendesk_org_id": int(zendesk_org["id"]),
            "zendesk_org_name": str(zendesk_org.get("name") or ""),
        }

    return desired, conflicts


def plan_reconciliation(
    desired_users: dict[str, dict[str, Any]],
    zendesk_users: list[dict[str, Any]],
    *,
    suspend_when_out_of_scope: bool = True,
    suspend_when_entra_disabled: bool = True,
    protect_zendesk_staff_roles: bool = True,
) -> list[dict[str, Any]]:
    """Compare desired Entra state to Zendesk and return a no-write action plan."""
    by_external: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_email: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for user in zendesk_users:
        external_id = str(user.get("external_id") or "").strip()
        if external_id:
            by_external[external_id].append(user)
        email = _norm_email(user.get("email"))
        if email:
            by_email[email].append(user)

    plan: list[dict[str, Any]] = []
    matched_zendesk_ids: set[int] = set()

    for entra_id, desired in desired_users.items():
        external_id = f"{EXTERNAL_ID_PREFIX}{entra_id}"
        external_matches = by_external.get(external_id, [])

        if len(external_matches) > 1:
            plan.append(
                _row(desired, "CONFLICT", "Multiple Zendesk users share this Entra external_id.")
            )
            continue

        zendesk_user: dict[str, Any] | None = None
        matched_by = ""

        if len(external_matches) == 1:
            zendesk_user = external_matches[0]
            matched_by = "external_id"
        else:
            email = desired["email"]
            email_matches = by_email.get(email, []) if email else []
            if len(email_matches) > 1:
                plan.append(
                    _row(desired, "CONFLICT", "Multiple Zendesk users match the Entra email address.")
                )
                continue
            if len(email_matches) == 1:
                zendesk_user = email_matches[0]
                matched_by = "email"

        if zendesk_user is None:
            if not desired["enabled"]:
                plan.append(
                    _row(desired, "NO CHANGE", "Entra account is disabled and no Zendesk user exists.")
                )
            else:
                plan.append(
                    _row(desired, "CREATE", "No Zendesk user matched by external_id or email.")
                )
            continue

        zendesk_id = int(zendesk_user.get("id"))
        matched_zendesk_ids.add(zendesk_id)
        role = str(zendesk_user.get("role") or "").strip().lower()
        if protect_zendesk_staff_roles and role in STAFF_ROLES:
            plan.append(
                _row(
                    desired,
                    "PROTECTED",
                    f"Matched Zendesk {role}; staff roles are protected from sync changes.",
                    zendesk_user,
                    matched_by,
                )
            )
            continue

        existing_external = str(zendesk_user.get("external_id") or "").strip()
        if matched_by == "email" and existing_external and existing_external != external_id:
            plan.append(
                _row(
                    desired,
                    "CONFLICT",
                    "Email matched a Zendesk user that already has a different external_id.",
                    zendesk_user,
                    matched_by,
                )
            )
            continue

        actions: list[str] = []
        reasons: list[str] = []

        if matched_by == "email" and not existing_external:
            actions.append("ADOPT")
            reasons.append(f"would set external_id to {external_id}")

        if not desired["enabled"] and suspend_when_entra_disabled:
            if not bool(zendesk_user.get("suspended")):
                actions.append("SUSPEND")
                reasons.append("Entra account is disabled")
        elif desired["enabled"]:
            if bool(zendesk_user.get("suspended")):
                actions.append("UNSUSPEND")
                reasons.append("Entra account is enabled and in provisioning scope")

            desired_email = desired["email"]
            zendesk_email = _norm_email(zendesk_user.get("email"))
            if desired_email and desired_email != zendesk_email:
                actions.append("UPDATE EMAIL")
                reasons.append(f"{zendesk_email or '-'} -> {desired_email}")

            desired_name = str(desired["name"] or "").strip()
            zendesk_name = str(zendesk_user.get("name") or "").strip()
            if desired_name and desired_name != zendesk_name:
                actions.append("UPDATE NAME")
                reasons.append(f"{zendesk_name or '-'} -> {desired_name}")

            current_org_id = zendesk_user.get("organization_id")
            if str(current_org_id or "") != str(desired["zendesk_org_id"]):
                actions.append("UPDATE ORGANIZATION")
                reasons.append(
                    f"organization {current_org_id or '-'} -> {desired['zendesk_org_id']}"
                )

        if not actions:
            actions = ["NO CHANGE"]
            reasons = ["Zendesk state already matches desired Entra state."]

        plan.append(
            _row(
                desired,
                " + ".join(actions),
                "; ".join(reasons),
                zendesk_user,
                matched_by,
            )
        )

    if suspend_when_out_of_scope:
        desired_ids = set(desired_users)
        for zendesk_user in zendesk_users:
            zendesk_id = int(zendesk_user.get("id"))
            if zendesk_id in matched_zendesk_ids:
                continue
            external_id = str(zendesk_user.get("external_id") or "").strip()
            if not external_id.startswith(EXTERNAL_ID_PREFIX):
                continue
            entra_id = external_id[len(EXTERNAL_ID_PREFIX) :]
            if not entra_id or entra_id in desired_ids:
                continue

            role = str(zendesk_user.get("role") or "").strip().lower()
            if protect_zendesk_staff_roles and role in STAFF_ROLES:
                plan.append(
                    {
                        "action": "PROTECTED",
                        "entra_id": entra_id,
                        "name": str(zendesk_user.get("name") or ""),
                        "email": _norm_email(zendesk_user.get("email")),
                        "zendesk_id": zendesk_id,
                        "matched_by": "external_id",
                        "reason": f"Linked Zendesk {role} is out of scope; staff roles are protected.",
                    }
                )
                continue

            if bool(zendesk_user.get("suspended")):
                action = "NO CHANGE"
                reason = "Linked Zendesk user is already suspended and is outside provisioning scope."
            else:
                action = "SUSPEND"
                reason = "Linked Zendesk user is no longer in any configured Entra group."
            plan.append(
                {
                    "action": action,
                    "entra_id": entra_id,
                    "name": str(zendesk_user.get("name") or ""),
                    "email": _norm_email(zendesk_user.get("email")),
                    "zendesk_id": zendesk_id,
                    "matched_by": "external_id",
                    "reason": reason,
                }
            )

    return sorted(plan, key=lambda row: (str(row.get("action")), str(row.get("name")).lower()))


def summarize_plan(rows: list[dict[str, Any]]) -> Counter[str]:
    """Count planned operation labels, including compound action rows."""
    counts: Counter[str] = Counter()
    for row in rows:
        action = str(row.get("action") or "")
        for part in (piece.strip() for piece in action.split("+")):
            if part:
                counts[part] += 1
    return counts


def _row(
    desired: dict[str, Any],
    action: str,
    reason: str,
    zendesk_user: dict[str, Any] | None = None,
    matched_by: str = "",
) -> dict[str, Any]:
    return {
        "action": action,
        "entra_id": desired["entra_id"],
        "name": desired["name"],
        "email": desired["email"],
        "group_name": desired["group_name"],
        "zendesk_org_id": desired["zendesk_org_id"],
        "zendesk_org_name": desired["zendesk_org_name"],
        "zendesk_id": int(zendesk_user.get("id")) if zendesk_user else None,
        "matched_by": matched_by,
        "reason": reason,
    }
