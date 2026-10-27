"""Recovery-only full operational reconcile apply.

This workflow rebuilds the same external-ID-only operational plan used by the
read-only full reconcile, applies every safe live Zendesk correction, and only
then advances the incremental Entra baseline. It exists for recovery from a
stale baseline where an incremental catch-up would not repair Zendesk drift
that is invisible in the Entra-to-Entra delta.
"""

from __future__ import annotations

from typing import Any

from lib.bootstrap_apply import (
    BootstrapApplyError,
    _apply_manager_second_pass,
    _expected_link_ids,
    _verify_after_apply,
    _write_row_first_pass,
)
from lib.cache import CacheError, load_entra_users_cache, save_entra_users_cache, save_zendesk_users_cache
from lib.config import ConfigError, load_config, validate_config
from lib.conflicts import ConflictSnapshotError, save_conflicts
from lib.graph import GraphError, get_graph_access_token, get_group_user_members, load_graph_config
from lib.reconcile import add_user_field_actions, build_desired_users, plan_reconciliation, summarize_plan
from lib.resolutions import ResolutionError, load_resolutions
from lib.zendesk import ZendeskError, get_access_token, get_users, load_zendesk_config

ZENDESK_APPLY_SCOPE = "users:read users:write"


class FullReconcileApplyError(RuntimeError):
    """Raised when a recovery full-reconcile apply cannot proceed safely."""


def _behavior(config: dict[str, Any], name: str, default: bool) -> bool:
    behavior = config.get("behavior") or {}
    return bool(behavior.get(name, default))


def _field_keys(config: dict[str, Any]) -> dict[str, str]:
    fields = ((config.get("zendesk") or {}).get("user_fields") or {})
    result = {
        "employee_id": str(fields.get("employee_id") or "").strip(),
        "job_title": str(fields.get("job_title") or "").strip(),
        "manager": str(fields.get("manager") or "").strip(),
    }
    if not all(result.values()):
        raise FullReconcileApplyError("Required Zendesk user-field keys are missing from config.yaml.")
    return result


def _action_parts(row: dict[str, Any]) -> set[str]:
    return {part.strip() for part in str(row.get("action") or "").split("+") if part.strip()}


def _normalize_current(desired_users: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    for entra_id, desired in desired_users.items():
        current[entra_id] = {
            "entra_id": entra_id,
            "name": str(desired.get("name") or "").strip(),
            "email": str(desired.get("email") or "").strip().lower(),
            "enabled": bool(desired.get("enabled")),
            "employee_id": str(desired.get("employee_id") or "").strip(),
            "job_title": str(desired.get("job_title") or "").strip(),
            "manager_entra_id": str(desired.get("manager_entra_id") or "").strip(),
            "manager_name": str(desired.get("manager_name") or "").strip(),
            "manager_email": str(desired.get("manager_email") or "").strip().lower(),
            "zendesk_org_id": int(desired["zendesk_org_id"]),
            "zendesk_org_name": str(desired.get("zendesk_org_name") or ""),
        }
    return current


def _print_summary(plan: list[dict[str, Any]]) -> None:
    counts = summarize_plan(plan)
    print("\nRecovery full-reconcile apply plan")
    print("----------------------------------")
    preferred = [
        "CREATE", "UPDATE EMAIL", "UPDATE NAME", "UPDATE ORGANIZATION",
        "UPDATE EMPLOYEE ID", "UPDATE JOB TITLE", "UPDATE MANAGER",
        "CLEAR MANAGER", "UNSUSPEND", "SUSPEND", "SKIP", "PROTECTED",
        "CONFLICT", "NO CHANGE",
    ]
    shown: set[str] = set()
    for action in preferred:
        if counts.get(action):
            print(f"{action:<22} {counts[action]:>5}")
            shown.add(action)
    for action in sorted(set(counts) - shown):
        print(f"{action:<22} {counts[action]:>5}")
    print(f"{'PLAN ROWS':<22} {len(plan):>5}")


def _build_live_plan() -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    str,
    dict[str, str],
]:
    print("\n[1/7] Loading configuration and saved conflict decisions...", flush=True)
    config = load_config()
    validate_config(config)
    mappings = list(config["mappings"])
    resolutions = load_resolutions()
    field_keys = _field_keys(config)
    print(f"      Configuration valid. {len(mappings)} group mapping(s) loaded.")
    print(f"      {len(resolutions)} saved conflict decision(s) loaded.")
    print("      Identity mode: OPERATIONAL (external_id only; email cannot transfer identity ownership).")

    print("\n[2/7] Authenticating to Microsoft Graph...", flush=True)
    graph_token = get_graph_access_token(load_graph_config())
    print("      Microsoft Graph authentication successful.")

    print("\n[3/7] Reading complete authoritative Entra scope...", flush=True)
    group_members: list[tuple[dict, list[dict]]] = []
    for index, mapping in enumerate(mappings, start=1):
        group = mapping.get("entra_group") or {}
        group_id = str(group.get("id") or "")
        group_name = str(group.get("name") or group_id)
        print(f"      [{index}/{len(mappings)}] {group_name}: reading direct user members + manager...", flush=True)
        members = get_group_user_members(graph_token, group_id)
        print(f"            {len(members)} user member(s) found.")
        group_members.append((mapping, members))

    desired_users, membership_rows, in_scope_ids = build_desired_users(
        group_members,
        resolutions=resolutions,
    )
    unresolved_memberships = [row for row in membership_rows if row.get("action") == "CONFLICT"]
    print(
        f"      {len(in_scope_ids)} unique Entra user(s) in scope; "
        f"{len(unresolved_memberships)} unresolved membership conflict(s)."
    )

    print(f"\n[4/7] Authenticating to Zendesk with exact scopes [{ZENDESK_APPLY_SCOPE}]...", flush=True)
    zendesk_config = load_zendesk_config()
    token, token_data = get_access_token(zendesk_config, scope=ZENDESK_APPLY_SCOPE)
    granted_scope = token_data.get("scope") or token_data.get("scopes") or "not reported"
    print(f"      Zendesk authentication successful. Granted scope: {granted_scope}")

    print("\n[5/7] Downloading a fresh complete Zendesk user snapshot...", flush=True)
    zendesk_users = get_users(token, zendesk_config["subdomain"])
    cache_path = save_zendesk_users_cache(zendesk_users, subdomain=zendesk_config["subdomain"])
    print(f"      {len(zendesk_users)} Zendesk user(s) loaded from live Zendesk.")
    print(f"      Local snapshot saved to: {cache_path}")

    print("\n[6/7] Building fresh external-ID-only full reconciliation plan...", flush=True)
    plan = plan_reconciliation(
        desired_users,
        zendesk_users,
        in_scope_entra_ids=in_scope_ids,
        suspend_when_out_of_scope=_behavior(config, "suspend_when_out_of_scope", True),
        suspend_when_entra_disabled=_behavior(config, "suspend_when_entra_disabled", True),
        protect_zendesk_staff_roles=_behavior(config, "protect_zendesk_staff_roles", True),
        resolutions=resolutions,
        allow_email_bootstrap=False,
    )
    plan = add_user_field_actions(plan, desired_users, zendesk_users, field_keys=field_keys)
    plan.extend(membership_rows)
    plan.sort(key=lambda row: (str(row.get("action")), str(row.get("name") or "").lower()))

    conflicts = [row for row in plan if row.get("action") == "CONFLICT"]
    conflict_path = save_conflicts(conflicts)
    print(f"      {len(conflicts)} unresolved conflict(s) saved to: {conflict_path}")

    if conflicts:
        raise FullReconcileApplyError(
            f"Refusing recovery apply because {len(conflicts)} unresolved conflict(s) remain."
        )
    if any("UPDATE EMAIL" in _action_parts(row) for row in plan):
        raise FullReconcileApplyError(
            "Refusing recovery apply because UPDATE EMAIL is present. Primary-email identity updates "
            "require the dedicated identity-safe path and are not enabled in this recovery workflow."
        )
    if any(_action_parts(row) & {"ADOPT", "RELINK"} for row in plan):
        raise FullReconcileApplyError(
            "Refusing recovery apply because an operational plan unexpectedly contains ADOPT/RELINK."
        )

    print("\n[7/7] Live recovery plan passed conflict and identity-safety validation.")
    return (
        config,
        desired_users,
        plan,
        zendesk_config,
        token,
        field_keys,
    )


def run_full_reconcile_apply() -> int:
    print("Entra -> Zendesk Sync (RECOVERY FULL-RECONCILE APPLY)")
    print("=====================================================")
    try:
        (
            _config,
            desired_users,
            plan,
            zendesk_config,
            token,
            field_keys,
        ) = _build_live_plan()

        _print_summary(plan)
        print(
            "\nThis recovery run will apply the fresh external-ID-only full reconciliation above. "
            "It will not use email to transfer identity ownership. The incremental baseline will "
            "advance only after all first-pass and manager writes succeed.\n"
            "Type APPLY exactly to continue, or press Enter to cancel."
        )
        try:
            confirmed = input("Confirmation: ").strip() == "APPLY"
        except (EOFError, KeyboardInterrupt):
            print()
            confirmed = False
        if not confirmed:
            print("Recovery full-reconcile apply cancelled. No Zendesk writes were started.")
            return 2

        actionable = [
            row
            for row in plan
            if _action_parts(row) - {"NO CHANGE", "SKIP", "PROTECTED", "UPDATE MANAGER", "CLEAR MANAGER"}
        ]
        print(
            f"\nFirst pass: applying identity-independent fields for {len(actionable)} actionable row(s)...",
            flush=True,
        )
        created = updated = skipped = 0
        for index, row in enumerate(plan, start=1):
            first_pass_actions = _action_parts(row) - {"UPDATE MANAGER", "CLEAR MANAGER"}
            if not first_pass_actions or first_pass_actions <= {"NO CHANGE", "SKIP", "PROTECTED"}:
                skipped += 1
                continue
            name = str(row.get("name") or row.get("email") or row.get("entra_id") or "-")
            print(f"      [{index}/{len(plan)}] {row.get('action')}: {name}...", flush=True)
            result = _write_row_first_pass(
                row,
                access_token=token,
                subdomain=zendesk_config["subdomain"],
                field_keys=field_keys,
            )
            if result == "CREATED":
                created += 1
            elif result == "UPDATED":
                updated += 1
            else:
                skipped += 1

        manager_updated, manager_unresolved = _apply_manager_second_pass(
            plan,
            access_token=token,
            subdomain=zendesk_config["subdomain"],
            manager_key=field_keys["manager"],
        )
        if manager_unresolved:
            raise FullReconcileApplyError(
                f"{manager_unresolved} manager relationship(s) could not be resolved; baseline was not advanced."
            )

        expected_link_ids = _expected_link_ids(plan)
        total_expected, missing_links = _verify_after_apply(
            expected_link_ids=expected_link_ids,
            access_token=token,
            subdomain=zendesk_config["subdomain"],
        )
        if missing_links:
            raise FullReconcileApplyError(
                f"{missing_links} of {total_expected} expected Entra external-ID links are missing; "
                "baseline was not advanced."
            )

        current = _normalize_current(desired_users)
        cache_path = save_entra_users_cache(current, previous=load_entra_users_cache())

        print("\nRecovery full-reconcile writes completed.")
        print(f"      Created: {created}")
        print(f"      First-pass updated: {updated}")
        print(f"      Manager relationships updated: {manager_updated}")
        print(f"      No-write/protected/skipped rows: {skipped}")
        print(f"      Successful operational Entra baseline saved atomically: {cache_path}")
        print("RECOVERY FULL-RECONCILE APPLY COMPLETE.")
        return 0

    except (
        BootstrapApplyError,
        CacheError,
        ConfigError,
        ConflictSnapshotError,
        FullReconcileApplyError,
        GraphError,
        ResolutionError,
        ZendeskError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}")
        print(
            "Operational baseline was not advanced. Any successful Zendesk writes remain and "
            "will be re-evaluated idempotently on the next live reconciliation."
        )
        return 1
