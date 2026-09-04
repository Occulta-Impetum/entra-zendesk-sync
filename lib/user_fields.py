"""Discovery and setup of Zendesk user fields managed by the sync."""

from __future__ import annotations

from typing import Any

from lib.config import save_config
from lib.zendesk import create_user_field, get_user_fields

JOB_TITLE_KEY = "standard::job_title"
MANAGER_KEY = "standard::manager"
EMPLOYEE_ID_DEFAULT_KEY = "employee_id"
EMPLOYEE_ID_TITLE = "Employee ID"


class UserFieldSetupError(RuntimeError):
    """Raised when required Zendesk user fields cannot be resolved safely."""


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _find_exact_key(fields: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    return next((field for field in fields if _norm(field.get("key")) == key.lower()), None)


def _employee_id_candidates(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for field in fields:
        title = _norm(field.get("title"))
        key = _norm(field.get("key"))
        if key == EMPLOYEE_ID_DEFAULT_KEY or title in {"employee id", "employee_id", "employee number"}:
            candidates.append(field)
    return candidates


def inspect_user_fields(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve required field definitions without writing anything."""
    title = _find_exact_key(fields, JOB_TITLE_KEY)
    manager = _find_exact_key(fields, MANAGER_KEY)
    if not title:
        raise UserFieldSetupError(f"Required Zendesk user field {JOB_TITLE_KEY!r} was not found.")
    if _norm(title.get("type")) != "text":
        raise UserFieldSetupError(
            f"Zendesk field {JOB_TITLE_KEY!r} exists but is type {title.get('type')!r}, expected 'text'."
        )

    if not manager:
        raise UserFieldSetupError(f"Required Zendesk user field {MANAGER_KEY!r} was not found.")
    if _norm(manager.get("type")) != "lookup" or _norm(manager.get("relationship_target_type")) != "zen:user":
        raise UserFieldSetupError(
            f"Zendesk field {MANAGER_KEY!r} must be a user lookup relationship (lookup -> zen:user)."
        )

    employee_candidates = _employee_id_candidates(fields)
    if len(employee_candidates) > 1:
        details = ", ".join(
            f"{field.get('title')} [{field.get('key')}]" for field in employee_candidates
        )
        raise UserFieldSetupError(
            "Multiple possible Employee ID user fields were found. Resolve the duplicate fields before continuing: "
            + details
        )
    employee = employee_candidates[0] if employee_candidates else None
    if employee and _norm(employee.get("type")) != "text":
        raise UserFieldSetupError(
            f"Employee ID field {employee.get('key')!r} exists but is type {employee.get('type')!r}, expected 'text'."
        )

    return {
        "job_title": title,
        "manager": manager,
        "employee_id": employee,
    }


def ensure_user_fields(
    *,
    config: dict[str, Any],
    access_token: str,
    subdomain: str,
    allow_create: bool,
) -> dict[str, str]:
    """Discover required fields and optionally create the missing Employee ID field.

    Standard Job Title and Manager fields must already exist because their special
    Zendesk behavior cannot be safely recreated as ordinary custom fields.
    """
    print("      Inspecting Zendesk user fields required by the sync...", flush=True)
    fields = get_user_fields(access_token, subdomain)
    resolved = inspect_user_fields(fields)

    employee = resolved["employee_id"]
    if employee is None:
        if not allow_create:
            print(
                f"      Employee ID field is missing. Bootstrap --apply will create text field "
                f"'{EMPLOYEE_ID_TITLE}' with key '{EMPLOYEE_ID_DEFAULT_KEY}'."
            )
            employee_key = EMPLOYEE_ID_DEFAULT_KEY
        else:
            print(
                f"      Employee ID field is missing; creating '{EMPLOYEE_ID_TITLE}' "
                f"[{EMPLOYEE_ID_DEFAULT_KEY}]...",
                flush=True,
            )
            employee = create_user_field(
                access_token,
                subdomain,
                title=EMPLOYEE_ID_TITLE,
                key=EMPLOYEE_ID_DEFAULT_KEY,
                field_type="text",
            )
            employee_key = str(employee.get("key") or EMPLOYEE_ID_DEFAULT_KEY)
            print(f"      Employee ID field created successfully [key: {employee_key}].")
    else:
        employee_key = str(employee.get("key") or EMPLOYEE_ID_DEFAULT_KEY)
        print(f"      Existing Employee ID field found [key: {employee_key}].")

    field_keys = {
        "employee_id": employee_key,
        "job_title": str(resolved["job_title"].get("key") or JOB_TITLE_KEY),
        "manager": str(resolved["manager"].get("key") or MANAGER_KEY),
    }

    zendesk_cfg = config.setdefault("zendesk", {})
    existing = zendesk_cfg.get("user_fields") or {}
    if existing != field_keys:
        zendesk_cfg["user_fields"] = field_keys
        path = save_config(config)
        print(f"      Saved resolved Zendesk user-field keys to: {path}")
    else:
        print("      Saved Zendesk user-field keys already match discovered schema.")

    return field_keys
