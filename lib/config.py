"""Configuration loading, validation, and persistence helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded, validated, or saved."""


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml, returning an empty dict when it does not exist."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return {}

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Unable to load configuration from {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be a mapping: {config_path}")
    return data


def save_config(config: dict[str, Any], path: str | Path | None = None) -> Path:
    """Validate and save non-secret configuration as YAML."""
    validate_config(config)
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with config_path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(
                config,
                handle,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Unable to save configuration to {config_path}: {exc}") from exc

    return config_path


def validate_config(config: dict[str, Any]) -> None:
    """Validate the fields required by the sync runtime."""
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a mapping.")

    mappings = config.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ConfigError("At least one Entra group to Zendesk organization mapping is required.")

    seen_groups: set[str] = set()
    for index, mapping in enumerate(mappings, start=1):
        if not isinstance(mapping, dict):
            raise ConfigError(f"Mapping {index} must be a mapping object.")
        entra_group = mapping.get("entra_group") or {}
        zendesk_org = mapping.get("zendesk_organization") or {}
        group_id = str(entra_group.get("id") or "").strip()
        org_id = zendesk_org.get("id")
        if not group_id:
            raise ConfigError(f"Mapping {index} is missing entra_group.id.")
        if org_id in (None, ""):
            raise ConfigError(f"Mapping {index} is missing zendesk_organization.id.")
        if group_id in seen_groups:
            raise ConfigError(f"Entra group {group_id} is mapped more than once.")
        seen_groups.add(group_id)


def build_config(
    *,
    tenant_id: str,
    client_id: str,
    zendesk_subdomain: str,
    mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the canonical non-secret configuration document."""
    return {
        "version": 1,
        "entra": {
            "tenant_id": tenant_id,
            "client_id": client_id,
        },
        "zendesk": {
            "subdomain": zendesk_subdomain,
            "default_role": "end-user",
        },
        "mappings": mappings,
        "behavior": {
            "suspend_when_out_of_scope": True,
            "suspend_when_entra_disabled": True,
            "ambiguous_group_membership": "conflict",
            "protect_zendesk_staff_roles": True,
            "dry_run_by_default": True,
            "max_removed_users_per_run": 10,
            "max_removed_percent_per_run": 5.0,
            "max_changed_users_per_run": 50,
            "max_changed_percent_per_run": 15.0,
        },
    }


def existing_mapping_by_group_id(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return existing mappings indexed by immutable Entra group ID."""
    result: dict[str, dict[str, Any]] = {}
    for mapping in config.get("mappings", []) if isinstance(config, dict) else []:
        if not isinstance(mapping, dict):
            continue
        entra_group = mapping.get("entra_group") or {}
        group_id = str(entra_group.get("id") or "").strip()
        if group_id:
            result[group_id] = mapping
    return result
