"""Operational write engine for incremental Entra -> Zendesk sync."""

from __future__ import annotations

from typing import Any

from lib.cache import CacheError, save_entra_users_cache
from lib.operational import OperationalError, build_incremental_plan
from lib.reconcile import EXTERNAL_ID_PREFIX
from lib.zendesk import (
    ZendeskError,
    create_user,
    find_users_by_email,
    find_users_by_external_id,
    get_access_token,
    get_user,
    load_zendesk_config,
    rename_primary_email_identity,
    update_user,
)

USER_WRITE_SCOPE = "users:read users:write"
# Zendesk currently documents User Identities as not supporting resource-scoped
# users:read/users:write. Request this broader scope only when an email-reuse
# repair actually needs to modify a primary identity.
IDENTITY_WRITE_SCOPE = "read write"


class OperationalApplyError(RuntimeError):
    """Raised when operational writes cannot continue safely."""


def _action_parts(row: dict[str, Any]) -> set[str]:
    return {part.strip() for part in str(row.get("action") or "").split("+") if part.strip()}


def _create_from_row(row: dict[str, Any], *, token: str, subdomain: str, field_keys: dict[str, str]) -> dict[str, Any]:
    user_fields = {
        field_keys["employee_id"]: str(row.get("employee_id") or "") or None,
        field_keys["job_title"]: str(row.get("job_title") or "") or None,
    }
    return create_user(
        token,
        subdomain,
        name=str(row.get("name") or "").strip(),
        email=str(row.get("email") or "").strip(),
        external_id=f"{EXTERNAL_ID_PREFIX}{row['entra_id']}",
        organization_id=int(row["zendesk_org_id"]),
        user_fields=user_fields,
    )


def repair_reused_email_and_create(
    row: dict[str, Any],
    *,
    user_token: str,
    identity_token: str,
    subdomain: str,
    field_keys: dict[str, str],
) -> dict[str, Any]:
    """Re-verify retirement, rename the old primary email, verify release, then create replacement."""
    old_zendesk_id = row.get("old_zendesk_id")
    old_entra_id = str(row.get("old_entra_id") or "").strip()
    old_alias = str(row.get("rename_old_email_to") or "").strip().lower()
    desired_email = str(row.get("email") or "").strip().lower()
    if old_zendesk_id is None or not old_entra_id or not old_alias or not desired_email:
        raise OperationalApplyError("Email-reuse plan row is missing repair metadata.")

    old_user = get_user(user_token, subdomain, int(old_zendesk_id))
    old_role = str(old_user.get("role") or "").lower()
    old_external = str(old_user.get("external_id") or "").strip().lower()
    expected_external = f"{EXTERNAL_ID_PREFIX}{old_entra_id}".lower()
    if old_role in {"admin", "agent"} or old_external != expected_external or not bool(old_user.get("suspended")):
        raise OperationalApplyError(
            "Email-reuse safety check failed immediately before rename: the old Zendesk identity is no longer "
            "a suspended managed end-user with the expected external_id. No email identity was changed."
        )

    current_owners = find_users_by_email(user_token, subdomain, desired_email)
    if len(current_owners) != 1 or int(current_owners[0].get("id") or 0) != int(old_zendesk_id):
        raise OperationalApplyError(
            f"Email-reuse safety check failed: {desired_email} is not owned exclusively by the expected retired Zendesk user."
        )
    if find_users_by_email(user_token, subdomain, old_alias):
        raise OperationalApplyError(f"Historical email alias {old_alias} became occupied before apply.")

    rename_primary_email_identity(identity_token, subdomain, int(old_zendesk_id), old_alias)

    still_owned = find_users_by_email(user_token, subdomain, desired_email)
    if still_owned:
        raise OperationalApplyError(
            f"Retired Zendesk identity was renamed, but {desired_email} is still in use; replacement user was not created."
        )

    return _create_from_row(row, token=user_token, subdomain=subdomain, field_keys=field_keys)


def _resolve_subject(row: dict[str, Any], *, token: str, subdomain: str) -> int:
    zendesk_id = row.get("zendesk_id")
    if zendesk_id is not None:
        return int(zendesk_id)
    entra_id = str(row.get("entra_id") or "").strip()
    if not entra_id:
        raise OperationalApplyError("Manager second-pass row is missing entra_id.")
    matches = find_users_by_external_id(token, subdomain, f"{EXTERNAL_ID_PREFIX}{entra_id}")
    if len(matches) != 1:
        raise OperationalApplyError(
            f"Manager second-pass subject {entra_id} resolved to {len(matches)} Zendesk users; expected exactly one."
        )
    return int(matches[0]["id"])


def _resolve_manager(row: dict[str, Any], *, token: str, subdomain: str) -> int:
    manager_entra_id = str(row.get("manager_entra_id") or "").strip()
    manager_email = str(row.get("manager_email") or "").strip().lower()
    if manager_entra_id:
        matches = find_users_by_external_id(token, subdomain, f"{EXTERNAL_ID_PREFIX}{manager_entra_id}")
        if len(matches) == 1:
            return int(matches[0]["id"])
        if len(matches) > 1:
            raise OperationalApplyError(
                f"Manager external_id entra:{manager_entra_id} matched multiple Zendesk users."
            )
    if manager_email:
        matches = find_users_by_email(token, subdomain, manager_email)
        if len(matches) == 1:
            return int(matches[0]["id"])
        if len(matches) > 1:
            raise OperationalApplyError(f"Manager email {manager_email} matched multiple Zendesk users.")
    raise OperationalApplyError(
        f"Manager target could not be resolved for {row.get('name') or row.get('entra_id')} after first-pass writes."
    )


def _preflight_manager_second_pass(
    plan: list[dict[str, Any]],
    *,
    token: str,
    subdomain: str,
) -> list[tuple[dict[str, Any], int, int | None]]:
    """Resolve every manager subject/target before making any manager-field writes."""
    resolved: list[tuple[dict[str, Any], int, int | None]] = []
    manager_rows = [
        row
        for row in plan
        if _action_parts(row) & {"UPDATE MANAGER", "CLEAR MANAGER"}
        and not (_action_parts(row) & {"PROTECTED", "CONFLICT"})
    ]
    if not manager_rows:
        return resolved
    print(f"\nManager second pass preflight: resolving {len(manager_rows)} relationship(s)...", flush=True)
    for index, row in enumerate(manager_rows, start=1):
        subject_id = _resolve_subject(row, token=token, subdomain=subdomain)
        manager_id = None
        if "UPDATE MANAGER" in _action_parts(row):
            manager_id = _resolve_manager(row, token=token, subdomain=subdomain)
        print(
            f"      [{index}/{len(manager_rows)}] {row.get('name') or row.get('entra_id')}: "
            f"subject {subject_id}, manager {manager_id if manager_id is not None else '-'}",
            flush=True,
        )
        resolved.append((row, subject_id, manager_id))
    return resolved


def execute_incremental_plan(
    plan: list[dict[str, Any]],
    current_state: dict[str, dict[str, Any]],
    previous_cache: dict[str, Any] | None,
    *,
    field_keys: dict[str, str],
) -> tuple[int, int]:
    """Execute a live incremental plan and commit cache only after all writes succeed."""
    conflicts = [row for row in plan if row.get("action") == "CONFLICT"]
    if conflicts:
        raise OperationalApplyError(f"Refusing operational apply: {len(conflicts)} conflict(s) remain.")

    if not plan:
        print("No authoritative Entra changes require Zendesk writes. Existing baseline remains unchanged.")
        return 0, 0

    zendesk_config = load_zendesk_config()
    subdomain = zendesk_config["subdomain"]
    print(f"Requesting operational Zendesk scopes [{USER_WRITE_SCOPE}]...", flush=True)
    user_token, _ = get_access_token(zendesk_config, scope=USER_WRITE_SCOPE)
    identity_token: str | None = None
    written = 0
    skipped = 0

    print("\nFirst pass: applying identity, organization, lifecycle, employee ID, and job title changes...", flush=True)
    for index, row in enumerate(plan, start=1):
        actions = _action_parts(row)
        name = str(row.get("name") or row.get("entra_id") or "-")
        if not actions or actions <= {"NO CHANGE", "PROTECTED", "UPDATE MANAGER", "CLEAR MANAGER"}:
            skipped += 1
            continue
        print(f"      [{index}/{len(plan)}] {row.get('action')}: {name}...", flush=True)

        if "EMAIL REUSE" in actions:
            if "CREATE" not in actions:
                raise OperationalApplyError("EMAIL REUSE row is missing CREATE action.")
            if identity_token is None:
                print(
                    f"      Email reuse requires User Identities API; requesting exact broader scopes [{IDENTITY_WRITE_SCOPE}]...",
                    flush=True,
                )
                identity_token, _ = get_access_token(zendesk_config, scope=IDENTITY_WRITE_SCOPE)
            repair_reused_email_and_create(
                row,
                user_token=user_token,
                identity_token=identity_token,
                subdomain=subdomain,
                field_keys=field_keys,
            )
            written += 1
            continue

        if "CREATE" in actions:
            _create_from_row(row, token=user_token, subdomain=subdomain, field_keys=field_keys)
            written += 1
            continue

        zendesk_id = row.get("zendesk_id")
        fields = row.get("fields_to_write") or {}
        if zendesk_id is None or not isinstance(fields, dict):
            raise OperationalApplyError(f"Operational row {name} is missing Zendesk update metadata.")
        if fields:
            update_user(user_token, subdomain, int(zendesk_id), fields=fields)
            written += 1
        else:
            skipped += 1

    resolved_managers = _preflight_manager_second_pass(plan, token=user_token, subdomain=subdomain)
    if resolved_managers:
        print("\nManager second pass: applying verified manager relationships...", flush=True)
        for index, (row, subject_id, manager_id) in enumerate(resolved_managers, start=1):
            print(
                f"      [{index}/{len(resolved_managers)}] {row.get('name') or row.get('entra_id')}: "
                f"manager -> {manager_id if manager_id is not None else '-'}...",
                flush=True,
            )
            update_user(
                user_token,
                subdomain,
                subject_id,
                fields={"user_fields": {field_keys["manager"]: manager_id}},
            )
            written += 1

    cache_path = save_entra_users_cache(current_state, previous=previous_cache)
    print(f"      Successful operational Entra baseline saved atomically: {cache_path}")
    return written, skipped


def run_operational_apply() -> int:
    """Build a fresh guarded plan and execute it; never applies a stale dry-run plan."""
    try:
        plan, current, previous = build_incremental_plan()
        from lib.config import load_config

        fields = (load_config().get("zendesk") or {}).get("user_fields") or {}
        field_keys = {
            "employee_id": str(fields.get("employee_id") or ""),
            "job_title": str(fields.get("job_title") or ""),
            "manager": str(fields.get("manager") or ""),
        }
        if not all(field_keys.values()):
            raise OperationalApplyError("Required Zendesk field keys are missing from config.yaml.")
        written, skipped = execute_incremental_plan(plan, current, previous, field_keys=field_keys)
        print(f"Operational apply complete. Zendesk writes: {written}; no-write/protected rows: {skipped}")
        return 0
    except (OperationalApplyError, OperationalError, CacheError, ZendeskError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}")
        print("Operational cache was not advanced. Any successful Zendesk writes remain and will be re-evaluated idempotently on the next run.")
        return 1


# Backward-compatible internal name while sync.py remains gated during validation.
run_operational_apply_unwired = run_operational_apply
