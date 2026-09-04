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


def _zendesk_candidate(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(user.get("id")) if user.get("id") is not None else None,
        "name": str(user.get("name") or ""),
        "email": _norm_email(user.get("email")),
        "external_id": str(user.get("external_id") or ""),
        "organization_id": user.get("organization_id"),
        "role": str(user.get("role") or ""),
        "suspended": bool(user.get("suspended")),
    }


def _desired_from_mapping(
    user_id: str,
    user: dict[str, Any],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    zendesk_org = mapping.get("zendesk_organization") or {}
    entra_group = mapping.get("entra_group") or {}
    return {
        "entra_id": user_id,
        "name": str(user.get("displayName") or "").strip(),
        "email": _entra_email(user),
        "enabled": bool(user.get("accountEnabled")),
        "group_id": str(entra_group.get("id") or ""),
        "group_name": str(entra_group.get("name") or ""),
        "zendesk_org_id": int(zendesk_org["id"]),
        "zendesk_org_name": str(zendesk_org.get("name") or ""),
    }


def build_desired_users(
    group_members: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    resolutions: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Build desired state, unresolved conflicts, and all in-scope Entra IDs."""
    resolutions = resolutions or {}
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
    in_scope_ids = set(memberships)

    for user_id, mappings in memberships.items():
        user = users_by_id[user_id]
        if len(mappings) != 1:
            resolution = resolutions.get(user_id, {})
            decision = str(resolution.get("decision") or "")
            selected_group_id = str(resolution.get("group_id") or "")

            if decision == "skip":
                conflicts.append(
                    {
                        "action": "SKIP",
                        "entra_id": user_id,
                        "name": str(user.get("displayName") or ""),
                        "email": _entra_email(user),
                        "reason": "Administrator chose to skip this in-scope Entra user.",
                        "conflict_type": "multiple_groups",
                    }
                )
                continue

            selected_mapping = next(
                (
                    mapping
                    for mapping in mappings
                    if str((mapping.get("entra_group") or {}).get("id") or "")
                    == selected_group_id
                ),
                None,
            )
            if decision == "use_group" and selected_mapping is not None:
                desired[user_id] = _desired_from_mapping(user_id, user, selected_mapping)
                continue

            group_candidates = []
            for mapping in mappings:
                entra_group = mapping.get("entra_group") or {}
                zendesk_org = mapping.get("zendesk_organization") or {}
                group_candidates.append(
                    {
                        "group_id": str(entra_group.get("id") or ""),
                        "group_name": str(entra_group.get("name") or ""),
                        "zendesk_org_id": zendesk_org.get("id"),
                        "zendesk_org_name": str(zendesk_org.get("name") or ""),
                    }
                )
            conflicts.append(
                {
                    "action": "CONFLICT",
                    "conflict_type": "multiple_groups",
                    "entra_id": user_id,
                    "name": str(user.get("displayName") or ""),
                    "email": _entra_email(user),
                    "reason": "User belongs to multiple mapped Entra groups.",
                    "groups": [item["group_name"] for item in group_candidates],
                    "group_candidates": group_candidates,
                }
            )
            continue

        desired[user_id] = _desired_from_mapping(user_id, user, mappings[0])

    return desired, conflicts, in_scope_ids


def plan_reconciliation(
    desired_users: dict[str, dict[str, Any]],
    zendesk_users: list[dict[str, Any]],
    *,
    in_scope_entra_ids: set[str] | None = None,
    suspend_when_out_of_scope: bool = True,
    suspend_when_entra_disabled: bool = True,
    protect_zendesk_staff_roles: bool = True,
    resolutions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compare desired Entra state to Zendesk and return a no-write action plan."""
    resolutions = resolutions or {}
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
        resolution = resolutions.get(entra_id, {})
        decision = str(resolution.get("decision") or "")
        external_id = f"{EXTERNAL_ID_PREFIX}{entra_id}"
        external_matches = by_external.get(external_id, [])

        if len(external_matches) > 1:
            if decision == "skip":
                plan.append(_row(desired, "SKIP", "Administrator chose to skip this Entra user."))
            else:
                row = _row(
                    desired,
                    "CONFLICT",
                    "Multiple Zendesk users share this Entra external_id.",
                )
                row["conflict_type"] = "multiple_external_id_matches"
                row["zendesk_candidates"] = [_zendesk_candidate(item) for item in external_matches]
                plan.append(row)
            continue

        zendesk_user: dict[str, Any] | None = None
        matched_by = ""
        forced_relink = False

        if len(external_matches) == 1:
            zendesk_user = external_matches[0]
            matched_by = "external_id"
        else:
            email = desired["email"]
            email_matches = by_email.get(email, []) if email else []
            if len(email_matches) > 1:
                selected_id = resolution.get("zendesk_user_id")
                selected = next(
                    (item for item in email_matches if str(item.get("id")) == str(selected_id)),
                    None,
                )
                if decision == "use_zendesk_user" and selected is not None:
                    zendesk_user = selected
                    matched_by = "email"
                elif decision == "skip":
                    plan.append(_row(desired, "SKIP", "Administrator chose to skip this Entra user."))
                    continue
                else:
                    row = _row(
                        desired,
                        "CONFLICT",
                        "Multiple Zendesk users match the Entra email address.",
                    )
                    row["conflict_type"] = "multiple_email_matches"
                    row["zendesk_candidates"] = [_zendesk_candidate(item) for item in email_matches]
                    plan.append(row)
                    continue
            elif len(email_matches) == 1:
                zendesk_user = email_matches[0]
                matched_by = "email"

        if zendesk_user is None:
            if decision == "skip":
                plan.append(_row(desired, "SKIP", "Administrator chose to skip this Entra user."))
            elif not desired["enabled"]:
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
            allowed_id = resolution.get("zendesk_user_id")
            allowed_match = allowed_id in (None, "") or str(allowed_id) == str(zendesk_id)
            if decision == "replace_external_id" and allowed_match:
                forced_relink = True
            elif decision == "skip":
                plan.append(
                    _row(
                        desired,
                        "SKIP",
                        "Administrator chose not to adopt the email-matched Zendesk user.",
                        zendesk_user,
                        matched_by,
                    )
                )
                continue
            else:
                row = _row(
                    desired,
                    "CONFLICT",
                    "Email matched a Zendesk user that already has a different external_id.",
                    zendesk_user,
                    matched_by,
                )
                row["conflict_type"] = "email_external_id_mismatch"
                row["zendesk_candidates"] = [_zendesk_candidate(zendesk_user)]
                plan.append(row)
                continue

        actions: list[str] = []
        reasons: list[str] = []

        if forced_relink:
            actions.append("RELINK")
            reasons.append(f"would replace external_id {existing_external} -> {external_id}")
        elif matched_by == "email" and not existing_external:
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
        protected_scope = in_scope_entra_ids if in_scope_entra_ids is not None else set(desired_users)
        for zendesk_user in zendesk_users:
            zendesk_id = int(zendesk_user.get("id"))
            if zendesk_id in matched_zendesk_ids:
                continue
            external_id = str(zendesk_user.get("external_id") or "").strip()
            if not external_id.startswith(EXTERNAL_ID_PREFIX):
                continue
            entra_id = external_id[len(EXTERNAL_ID_PREFIX) :]
            if not entra_id or entra_id in protected_scope:
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
    row = {
        "action": action,
        "entra_id": desired["entra_id"],
        "name": desired["name"],
        "email": desired["email"],
        "group_id": desired.get("group_id", ""),
        "group_name": desired["group_name"],
        "zendesk_org_id": desired["zendesk_org_id"],
        "zendesk_org_name": desired["zendesk_org_name"],
        "zendesk_id": int(zendesk_user.get("id")) if zendesk_user else None,
        "matched_by": matched_by,
        "reason": reason,
    }
    if zendesk_user:
        row["zendesk_name"] = str(zendesk_user.get("name") or "")
        row["zendesk_email"] = _norm_email(zendesk_user.get("email"))
        row["zendesk_external_id"] = str(zendesk_user.get("external_id") or "")
    return row
