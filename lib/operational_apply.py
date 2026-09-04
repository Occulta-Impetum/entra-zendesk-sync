"""Operational write engine for incremental Entra -> Zendesk sync.

This module is intentionally not wired to sync.py --apply yet. It exists so the
incremental and reused-email write paths can be unit-tested before production
apply is enabled.
"""

from __future__ import annotations

from typing import Any

from lib.cache import save_entra_users_cache
from lib.operational import OperationalError, build_incremental_plan
from lib.reconcile import EXTERNAL_ID_PREFIX
from lib.zendesk import (
    ZendeskError,
    create_user,
    find_users_by_email,
    get_access_token,
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
    """Rename retired user's primary email, verify release, then create replacement."""
    old_zendesk_id = row.get("old_zendesk_id")
    old_alias = str(row.get("rename_old_email_to") or "").strip().lower()
    desired_email = str(row.get("email") or "").strip().lower()
    if old_zendesk_id is None or not old_alias or not desired_email:
        raise OperationalApplyError("Email-reuse plan row is missing repair metadata.")

    rename_primary_email_identity(
        identity_token,
        subdomain,
        int(old_zendesk_id),
        old_alias,
    )

    still_owned = find_users_by_email(user_token, subdomain, desired_email)
    if still_owned:
        raise OperationalApplyError(
            f"Retired Zendesk identity was renamed, but {desired_email} is still in use; replacement user was not created."
        )

    return _create_from_row(row, token=user_token, subdomain=subdomain, field_keys=field_keys)


def execute_incremental_plan(
    plan: list[dict[str, Any]],
    current_state: dict[str, dict[str, Any]],
    previous_cache: dict[str, Any] | None,
    *,
    field_keys: dict[str, str],
) -> tuple[int, int]:
    """Execute a previously rebuilt incremental plan and commit cache only on success."""
    conflicts = [row for row in plan if row.get("action") == "CONFLICT"]
    if conflicts:
        raise OperationalApplyError(f"Refusing operational apply: {len(conflicts)} conflict(s) remain.")

    zendesk_config = load_zendesk_config()
    subdomain = zendesk_config["subdomain"]
    user_token, _ = get_access_token(zendesk_config, scope=USER_WRITE_SCOPE)
    identity_token: str | None = None
    written = 0
    skipped = 0

    for index, row in enumerate(plan, start=1):
        action = str(row.get("action") or "")
        name = str(row.get("name") or row.get("entra_id") or "-")
        if action in {"NO CHANGE", "PROTECTED"}:
            skipped += 1
            continue
        print(f"      [{index}/{len(plan)}] {action}: {name}...", flush=True)

        if action == "CREATE":
            _create_from_row(row, token=user_token, subdomain=subdomain, field_keys=field_keys)
            written += 1
            continue

        if action == "EMAIL REUSE + CREATE":
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

        zendesk_id = row.get("zendesk_id")
        fields = row.get("fields_to_write") or {}
        if zendesk_id is None or not isinstance(fields, dict):
            raise OperationalApplyError(f"Operational row {name} is missing Zendesk update metadata.")
        if fields:
            update_user(user_token, subdomain, int(zendesk_id), fields=fields)
            written += 1
        else:
            skipped += 1

    cache_path = save_entra_users_cache(current_state, previous=previous_cache)
    print(f"      Successful operational Entra baseline saved: {cache_path}")
    return written, skipped


def run_operational_apply_unwired() -> int:
    """Build and execute the live plan. Not exposed by sync.py until validation is complete."""
    try:
        plan, current, previous = build_incremental_plan()
        # Config field keys are already embedded in fields_to_write for updates, but
        # CREATE rows need canonical keys. Read them from the validated config lazily.
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
        print(f"Operational writes complete. Written: {written}; no-write/protected: {skipped}")
        return 0
    except (OperationalApplyError, OperationalError, ZendeskError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}")
        print("Operational cache was not advanced. Any successful Zendesk writes remain and will be re-evaluated on the next run.")
        return 1
