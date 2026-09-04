"""Incremental operational planning for Entra -> Zendesk synchronization.

Normal scheduled runs use Entra as the change detector and query Zendesk only for
users whose authoritative Entra state changed. A separate full reconciliation
path remains available for administrators who intentionally want to overwrite
manual Zendesk drift.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from lib.cache import CacheError, diff_entra_users, load_entra_users_cache
from lib.config import ConfigError, load_config, validate_config
from lib.graph import GraphError, get_graph_access_token, get_group_user_members, get_user_managers, load_graph_config
from lib.reconcile import EXTERNAL_ID_PREFIX, build_desired_users
from lib.resolutions import ResolutionError, load_resolutions
from lib.zendesk import (
    ZendeskError,
    find_users_by_email,
    find_users_by_external_id,
    get_access_token,
    get_user,
    load_zendesk_config,
)

READ_SCOPE = "users:read"


class OperationalError(RuntimeError):
    """Raised when incremental operational planning cannot proceed safely."""


def _field_keys(config: dict[str, Any]) -> dict[str, str]:
    fields = (config.get("zendesk") or {}).get("user_fields") or {}
    required = {
        "employee_id": str(fields.get("employee_id") or "").strip(),
        "job_title": str(fields.get("job_title") or "").strip(),
        "manager": str(fields.get("manager") or "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise OperationalError(
            "Zendesk user-field configuration is incomplete. Run bootstrap/setup first. Missing: "
            + ", ".join(missing)
        )
    return required


def _normalize_current(
    desired_users: dict[str, dict[str, Any]],
    managers: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for entra_id, desired in desired_users.items():
        manager = managers.get(entra_id) or {}
        current[entra_id] = {
            "entra_id": entra_id,
            "name": str(desired.get("name") or "").strip(),
            "email": str(desired.get("email") or "").strip().lower(),
            "enabled": bool(desired.get("enabled")),
            "employee_id": str(desired.get("employee_id") or "").strip(),
            "job_title": str(desired.get("job_title") or "").strip(),
            "manager_entra_id": str(manager.get("id") or "").strip(),
            "manager_name": str(manager.get("displayName") or "").strip(),
            "manager_email": str(manager.get("mail") or manager.get("userPrincipalName") or "").strip().lower(),
            "zendesk_org_id": int(desired["zendesk_org_id"]),
            "zendesk_org_name": str(desired.get("zendesk_org_name") or ""),
        }
    return current


def collect_current_entra_state() -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Collect the complete in-scope authoritative Entra snapshot with visible progress."""
    config = load_config()
    validate_config(config)
    _field_keys(config)
    mappings = list(config["mappings"])
    resolutions = load_resolutions()

    print("      Authenticating to Microsoft Graph...", flush=True)
    graph_token = get_graph_access_token(load_graph_config())
    group_members: list[tuple[dict, list[dict]]] = []
    all_ids: set[str] = set()
    for index, mapping in enumerate(mappings, start=1):
        group = mapping.get("entra_group") or {}
        group_id = str(group.get("id") or "")
        group_name = str(group.get("name") or group_id)
        print(f"      [{index}/{len(mappings)}] {group_name}: reading direct user members...", flush=True)
        members = get_group_user_members(graph_token, group_id)
        print(f"            {len(members)} user member(s) found.")
        group_members.append((mapping, members))
        all_ids.update(str(user.get("id") or "") for user in members if user.get("id"))

    desired, membership_rows, _in_scope = build_desired_users(group_members, resolutions=resolutions)
    unresolved = [row for row in membership_rows if row.get("action") == "CONFLICT"]
    if unresolved:
        return config, {}, unresolved

    managers = get_user_managers(graph_token, set(desired))
    current = _normalize_current(desired, managers)
    return config, current, membership_rows


def _changed_fields(old: dict[str, Any] | None, new: dict[str, Any]) -> set[str]:
    if old is None:
        return {"new"}
    return {key for key in new if key != "entra_id" and old.get(key) != new.get(key)}


def _collision_email(original: str, employee_id: str) -> str:
    local, sep, domain = original.partition("@")
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "", employee_id)
    if not sep or not local or not domain or not safe_id:
        raise OperationalError(
            f"Cannot generate historical email alias from {original!r} and employee ID {employee_id!r}."
        )
    return f"{local}{safe_id}@{domain}".lower()


def _history_record(cache: dict[str, Any] | None, entra_id: str) -> dict[str, Any] | None:
    if not cache or not isinstance(cache.get("history"), dict):
        return None
    value = cache["history"].get(entra_id)
    return value if isinstance(value, dict) else None


def _resolve_manager_target(
    record: dict[str, Any],
    *,
    token: str,
    subdomain: str,
) -> tuple[int | None, str]:
    manager_id = str(record.get("manager_entra_id") or "").strip()
    manager_email = str(record.get("manager_email") or "").strip().lower()
    if manager_id:
        matches = find_users_by_external_id(token, subdomain, f"{EXTERNAL_ID_PREFIX}{manager_id}")
        if len(matches) == 1:
            return int(matches[0]["id"]), "manager external_id"
        if len(matches) > 1:
            return None, "multiple Zendesk users share manager external_id"
    if manager_email:
        matches = find_users_by_email(token, subdomain, manager_email)
        if len(matches) == 1:
            return int(matches[0]["id"]), "manager email fallback (relationship resolution only)"
        if len(matches) > 1:
            return None, "multiple Zendesk users match manager email"
    return None, "manager Zendesk identity not found"


def build_incremental_plan() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Build a targeted operational plan without downloading every Zendesk user."""
    print("\n[1/4] Collecting fresh authoritative Entra state...", flush=True)
    config, current, membership_rows = collect_current_entra_state()
    if any(row.get("action") == "CONFLICT" for row in membership_rows):
        return membership_rows, current, load_entra_users_cache()

    previous_cache = load_entra_users_cache()
    new_ids, changed_ids, removed_ids = diff_entra_users(current, previous_cache)
    previous_current = previous_cache.get("current", {}) if previous_cache else {}
    print(
        f"      Entra change set: {len(new_ids)} new, {len(changed_ids)} changed, "
        f"{len(removed_ids)} removed from provisioning scope."
    )
    if previous_cache is None:
        print("      No prior Entra cache exists; this first operational run will inspect all current users once.")

    print("\n[2/4] Authenticating to Zendesk with read-only user scope...", flush=True)
    zendesk_config = load_zendesk_config()
    token, token_data = get_access_token(zendesk_config, scope=READ_SCOPE)
    granted = token_data.get("scope") or token_data.get("scopes") or "not reported"
    print(f"      Zendesk authentication successful. Granted scope: {granted}")

    field_keys = _field_keys(config)
    changed_current_ids = sorted(new_ids | changed_ids)
    plan: list[dict[str, Any]] = []

    print(f"\n[3/4] Inspecting {len(changed_current_ids)} changed/current Zendesk identity target(s)...", flush=True)
    for index, entra_id in enumerate(changed_current_ids, start=1):
        record = current[entra_id]
        old = previous_current.get(entra_id) if isinstance(previous_current, dict) else None
        delta = _changed_fields(old if isinstance(old, dict) else None, record)
        print(f"      [{index}/{len(changed_current_ids)}] {record['name']} <{record['email']}>...", flush=True)
        external_id = f"{EXTERNAL_ID_PREFIX}{entra_id}"
        matches = find_users_by_external_id(token, zendesk_config["subdomain"], external_id)
        if len(matches) > 1:
            plan.append({**record, "action": "CONFLICT", "reason": "Multiple Zendesk users share this Entra external_id."})
            continue

        if len(matches) == 0:
            email_matches = find_users_by_email(token, zendesk_config["subdomain"], record["email"])
            if not email_matches:
                plan.append({**record, "action": "CREATE", "reason": "No Zendesk user has this external_id or email.", "changed_fields": sorted(delta)})
                continue
            if len(email_matches) > 1:
                plan.append({**record, "action": "CONFLICT", "reason": "Multiple Zendesk users currently own the desired email address."})
                continue

            owner = email_matches[0]
            old_external = str(owner.get("external_id") or "").strip()
            old_entra_id = old_external[len(EXTERNAL_ID_PREFIX):] if old_external.lower().startswith(EXTERNAL_ID_PREFIX) else ""
            old_record = _history_record(previous_cache, old_entra_id) if old_entra_id else None
            if (
                old_entra_id
                and old_entra_id != entra_id
                and old_entra_id not in current
                and old_record
                and str(old_record.get("employee_id") or "").strip()
            ):
                historical_email = _collision_email(record["email"], str(old_record["employee_id"]))
                existing_alias = find_users_by_email(token, zendesk_config["subdomain"], historical_email)
                if existing_alias:
                    plan.append({**record, "action": "CONFLICT", "reason": f"Historical email alias {historical_email} is already in use."})
                    continue
                plan.append({
                    **record,
                    "action": "EMAIL REUSE + CREATE",
                    "reason": "Desired email belongs to a previously managed Entra identity that is no longer present.",
                    "old_zendesk_id": int(owner["id"]),
                    "old_entra_id": old_entra_id,
                    "old_employee_id": str(old_record["employee_id"]),
                    "rename_old_email_to": historical_email,
                    "changed_fields": sorted(delta),
                })
                continue

            plan.append({
                **record,
                "action": "CONFLICT",
                "reason": "Desired email is already owned by a Zendesk identity that cannot be proven to be a retired Entra user.",
                "zendesk_id": int(owner["id"]),
                "zendesk_external_id": old_external,
            })
            continue

        zendesk_user = get_user(token, zendesk_config["subdomain"], int(matches[0]["id"]))
        role = str(zendesk_user.get("role") or "").lower()
        if role in {"admin", "agent"}:
            plan.append({**record, "action": "PROTECTED", "zendesk_id": int(zendesk_user["id"]), "reason": f"Matched Zendesk {role}; staff roles are protected."})
            continue

        actions: list[str] = []
        fields_to_write: dict[str, Any] = {}
        if "new" in delta or "name" in delta:
            fields_to_write["name"] = record["name"]
            actions.append("UPDATE NAME")
        if "new" in delta or "zendesk_org_id" in delta:
            fields_to_write["organization_id"] = record["zendesk_org_id"]
            actions.append("UPDATE ORGANIZATION")
        if "new" in delta or "enabled" in delta:
            fields_to_write["suspended"] = not record["enabled"]
            actions.append("UNSUSPEND" if record["enabled"] else "SUSPEND")

        user_fields: dict[str, Any] = {}
        if "new" in delta or "employee_id" in delta:
            user_fields[field_keys["employee_id"]] = record["employee_id"] or None
            actions.append("UPDATE EMPLOYEE ID")
        if "new" in delta or "job_title" in delta:
            user_fields[field_keys["job_title"]] = record["job_title"] or None
            actions.append("UPDATE JOB TITLE")
        if user_fields:
            fields_to_write["user_fields"] = user_fields

        manager_target = None
        manager_reason = ""
        if "new" in delta or "manager_entra_id" in delta or "manager_email" in delta:
            if record["manager_entra_id"] or record["manager_email"]:
                manager_target, manager_reason = _resolve_manager_target(record, token=token, subdomain=zendesk_config["subdomain"])
                if manager_target is None:
                    plan.append({**record, "action": "CONFLICT", "zendesk_id": int(zendesk_user["id"]), "reason": manager_reason})
                    continue
                user_fields = fields_to_write.setdefault("user_fields", {})
                user_fields[field_keys["manager"]] = str(manager_target)
                actions.append("UPDATE MANAGER")
            else:
                user_fields = fields_to_write.setdefault("user_fields", {})
                user_fields[field_keys["manager"]] = None
                actions.append("CLEAR MANAGER")

        plan.append({
            **record,
            "action": " + ".join(actions) if actions else "NO CHANGE",
            "zendesk_id": int(zendesk_user["id"]),
            "reason": "Authoritative Entra fields changed since the last successful operational snapshot." if actions else "No authoritative Entra fields changed.",
            "changed_fields": sorted(delta),
            "fields_to_write": fields_to_write,
            "manager_resolution": manager_reason,
        })

    print(f"\n[4/4] Inspecting {len(removed_ids)} Entra user(s) removed from provisioning scope...", flush=True)
    for index, entra_id in enumerate(sorted(removed_ids), start=1):
        old_record = previous_current.get(entra_id, {}) if isinstance(previous_current, dict) else {}
        print(f"      [{index}/{len(removed_ids)}] {old_record.get('name') or entra_id}...", flush=True)
        matches = find_users_by_external_id(token, zendesk_config["subdomain"], f"{EXTERNAL_ID_PREFIX}{entra_id}")
        if len(matches) == 1:
            plan.append({
                **old_record,
                "entra_id": entra_id,
                "action": "SUSPEND",
                "zendesk_id": int(matches[0]["id"]),
                "reason": "Previously managed Entra identity is no longer in provisioning scope.",
                "fields_to_write": {"suspended": True},
            })
        elif len(matches) > 1:
            plan.append({**old_record, "entra_id": entra_id, "action": "CONFLICT", "reason": "Multiple Zendesk users share the removed Entra external_id."})
        else:
            plan.append({**old_record, "entra_id": entra_id, "action": "NO CHANGE", "reason": "Removed Entra identity has no linked Zendesk user."})

    return plan, current, previous_cache


def summarize_incremental_plan(plan: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in plan:
        action = str(row.get("action") or "")
        if action == "EMAIL REUSE + CREATE":
            counts["EMAIL REUSE"] += 1
            counts["CREATE"] += 1
            continue
        for part in [piece.strip() for piece in action.split("+") if piece.strip()]:
            counts[part] += 1
    return counts


def print_incremental_plan(plan: list[dict[str, Any]]) -> None:
    counts = summarize_incremental_plan(plan)
    print("\nIncremental reconciliation summary")
    print("----------------------------------")
    for action in (
        "CREATE", "EMAIL REUSE", "UPDATE NAME", "UPDATE ORGANIZATION", "UPDATE EMPLOYEE ID",
        "UPDATE JOB TITLE", "UPDATE MANAGER", "CLEAR MANAGER", "UNSUSPEND", "SUSPEND",
        "PROTECTED", "CONFLICT", "NO CHANGE",
    ):
        if counts.get(action):
            print(f"{action:<22} {counts[action]:>5}")
    print(f"{'PLAN ROWS':<22} {len(plan):>5}")

    print("\nDetailed incremental plan")
    print("-------------------------")
    for row in plan:
        print(f"[{row.get('action')}] {row.get('name') or '-'} <{row.get('email') or '-'}>")
        if row.get("rename_old_email_to"):
            print(f"  Retired Zendesk email would be renamed to: {row['rename_old_email_to']}")
        if row.get("reason"):
            print(f"  Reason: {row['reason']}")
        changed = row.get("changed_fields") or []
        if changed:
            print("  Entra changes: " + ", ".join(str(item) for item in changed))
        print()


def run_incremental_dry_run() -> int:
    print("Entra -> Zendesk Sync (OPERATIONAL INCREMENTAL DRY RUN)")
    print("=====================================================")
    try:
        plan, _current, _previous = build_incremental_plan()
    except (OperationalError, CacheError, ConfigError, GraphError, ResolutionError, ZendeskError, KeyError, ValueError) as exc:
        print(f"\nERROR: {exc}")
        return 1
    print_incremental_plan(plan)
    conflicts = [row for row in plan if row.get("action") == "CONFLICT"]
    print("OPERATIONAL INCREMENTAL DRY RUN COMPLETE: no Zendesk data or Entra cache was modified.")
    if conflicts:
        print(f"This plan is NOT ready for apply because {len(conflicts)} conflict(s) remain.")
    if load_entra_users_cache() is None:
        print("No successful operational Entra baseline exists yet; all current users were inspected this run.")
    else:
        print("Only Entra identities that changed since the last successful snapshot were inspected in Zendesk.")
    return 0
