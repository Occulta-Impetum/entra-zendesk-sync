"""Shared read-only runtime for bootstrap and operational Entra -> Zendesk sync flows."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from lib.bootstrap_review import (
    BootstrapReviewError,
    build_review_candidates,
    load_review_decisions,
    save_review_candidates,
    unresolved_review_candidates,
)
from lib.cache import CacheError, load_zendesk_users_cache, save_zendesk_users_cache
from lib.config import ConfigError, load_config, validate_config
from lib.conflicts import ConflictSnapshotError, save_conflicts
from lib.graph import (
    GraphError,
    get_graph_access_token,
    get_group_user_members,
    load_graph_config,
)
from lib.logging_utils import print_attention
from lib.reconcile import (
    add_user_field_actions,
    build_desired_users,
    plan_reconciliation,
    summarize_plan,
)
from lib.resolutions import ResolutionError, load_resolutions
from lib.user_fields import UserFieldSetupError, ensure_user_fields
from lib.zendesk import ZendeskError, get_access_token, get_users, load_zendesk_config

ZENDESK_DRY_RUN_SCOPE = "users:read"
ZENDESK_BOOTSTRAP_DRY_RUN_SCOPE = "users:read account_settings:read"


@dataclass(frozen=True)
class RuntimeOptions:
    label: str
    allow_email_bootstrap: bool
    force_refresh: bool = False
    include_bootstrap_review: bool = False


def _behavior(config: dict, name: str, default: bool) -> bool:
    behavior = config.get("behavior") or {}
    return bool(behavior.get(name, default))


def _configured_field_keys(config: dict) -> dict[str, str]:
    fields = ((config.get("zendesk") or {}).get("user_fields") or {})
    required = ("employee_id", "job_title", "manager")
    missing = [key for key in required if not str(fields.get(key) or "").strip()]
    if missing:
        raise ConfigError(
            "Zendesk user-field mappings are not configured. Run setup/bootstrap_sync.py first. "
            "Missing: " + ", ".join(missing)
        )
    return {key: str(fields[key]) for key in required}


def _print_summary(counts: Counter[str], total_rows: int) -> None:
    print("\nReconciliation summary")
    print("----------------------")
    preferred_order = [
        "CREATE", "ADOPT", "RELINK", "UPDATE EMAIL", "UPDATE NAME",
        "UPDATE ORGANIZATION", "UPDATE EMPLOYEE ID", "UPDATE JOB TITLE",
        "UPDATE MANAGER", "CLEAR MANAGER", "UNSUSPEND", "SUSPEND",
        "SKIP", "PROTECTED", "CONFLICT", "NO CHANGE",
    ]
    shown: set[str] = set()
    for action in preferred_order:
        if counts.get(action):
            print(f"{action:<22} {counts[action]:>5}")
            shown.add(action)
    for action in sorted(set(counts) - shown):
        print(f"{action:<22} {counts[action]:>5}")
    print(f"{'PLAN ROWS':<22} {total_rows:>5}")


def _print_details(rows: list[dict]) -> None:
    print("\nDetailed plan")
    print("-------------")
    for row in rows:
        action = str(row.get("action") or "")
        name = str(row.get("name") or "-")
        email = str(row.get("email") or "-")
        print(f"[{action}] {name} <{email}>")
        group = str(row.get("group_name") or "")
        org = str(row.get("zendesk_org_name") or "")
        matched_by = str(row.get("matched_by") or "")
        zendesk_id = row.get("zendesk_id")
        reason = str(row.get("reason") or "")
        groups = row.get("groups") or []
        if group:
            print(f"  Entra group: {group}")
        if org:
            print(f"  Desired Zendesk org: {org} [ID: {row.get('zendesk_org_id')}]")
        if zendesk_id:
            match_text = f" via {matched_by}" if matched_by else ""
            print(f"  Zendesk user: {zendesk_id}{match_text}")
        if groups:
            print("  Conflicting groups: " + ", ".join(str(item) for item in groups))
        if reason:
            print(f"  Reason: {reason}")
        print()


def _load_or_refresh_zendesk_users(
    *, access_token: str, subdomain: str, force_refresh: bool
) -> tuple[list[dict], str]:
    if not force_refresh:
        cached = load_zendesk_users_cache(subdomain=subdomain)
        if cached is not None:
            users, metadata = cached
            print(f"      Using local Zendesk user snapshot: {metadata['path']}")
            print(f"      Snapshot fetched at: {metadata['fetched_at']}")
            print(f"      {len(users)} Zendesk user(s) loaded from cache.")
            return users, "cache"
    print("      Downloading a fresh Zendesk user snapshot...", flush=True)
    users = get_users(access_token, subdomain)
    cache_path = save_zendesk_users_cache(users, subdomain=subdomain)
    print(f"      {len(users)} Zendesk user(s) loaded from Zendesk.")
    print(f"      Local snapshot saved to: {cache_path}")
    return users, "live"


def run_read_only(options: RuntimeOptions) -> int:
    print(f"Entra -> Zendesk Sync ({options.label})")
    print("=" * (24 + len(options.label)))
    try:
        print("\n[1/7] Loading configuration and review decisions...", flush=True)
        config = load_config()
        validate_config(config)
        mappings = list(config["mappings"])
        resolutions = load_resolutions()
        bootstrap_decisions = load_review_decisions() if options.include_bootstrap_review else {}
        print(f"      Configuration valid. {len(mappings)} group mapping(s) loaded.")
        print(f"      {len(resolutions)} saved conflict decision(s) loaded.")
        if options.include_bootstrap_review:
            print(f"      {len(bootstrap_decisions)} saved initial-match review decision(s) loaded.")
        print(
            "      Identity mode: INITIAL SETUP (external_id first, then exact email bootstrap)."
            if options.allow_email_bootstrap
            else "      Identity mode: OPERATIONAL (external_id only; missing external_id => CREATE)."
        )

        print("\n[2/7] Authenticating to Microsoft Graph...", flush=True)
        graph_config = load_graph_config()
        graph_token = get_graph_access_token(graph_config)
        print("      Microsoft Graph authentication successful.")

        print("\n[3/7] Reading Entra users, employee data, and managers inline...", flush=True)
        group_members: list[tuple[dict, list[dict]]] = []
        for index, mapping in enumerate(mappings, start=1):
            entra_group = mapping.get("entra_group") or {}
            group_id = str(entra_group.get("id") or "")
            group_name = str(entra_group.get("name") or group_id)
            print(f"      [{index}/{len(mappings)}] {group_name}: reading direct user members + manager...", flush=True)
            members = get_group_user_members(graph_token, group_id)
            print(f"            {len(members)} user member(s) found.")
            group_members.append((mapping, members))

        desired_users, membership_conflicts, in_scope_ids = build_desired_users(
            group_members, resolutions=resolutions
        )
        unresolved_memberships = sum(1 for row in membership_conflicts if row.get("action") == "CONFLICT")
        print(
            f"      {len(in_scope_ids)} unique Entra user(s) in scope; "
            f"{unresolved_memberships} unresolved multiple-group conflict(s)."
        )

        print("\n[4/7] Authenticating to Zendesk with required read-only scope...", flush=True)
        zendesk_config = load_zendesk_config()
        requested_scope = ZENDESK_BOOTSTRAP_DRY_RUN_SCOPE if options.include_bootstrap_review else ZENDESK_DRY_RUN_SCOPE
        print(f"      Requested scope: {requested_scope}")
        zendesk_token, token_data = get_access_token(zendesk_config, scope=requested_scope)
        granted_scope = token_data.get("scope") or token_data.get("scopes") or "not reported"
        print(f"      Zendesk authentication successful. Granted scope: {granted_scope}")

        print("\n[5/7] Checking Zendesk user-field schema...", flush=True)
        if options.include_bootstrap_review:
            field_keys = ensure_user_fields(
                config=config,
                access_token=zendesk_token,
                subdomain=zendesk_config["subdomain"],
                allow_create=False,
            )
        else:
            field_keys = _configured_field_keys(config)
            print(
                "      Using configured fields: "
                f"Employee ID={field_keys['employee_id']}, Job Title={field_keys['job_title']}, "
                f"Manager={field_keys['manager']}"
            )

        print("\n[6/7] Loading Zendesk users...", flush=True)
        zendesk_users, zendesk_source = _load_or_refresh_zendesk_users(
            access_token=zendesk_token,
            subdomain=zendesk_config["subdomain"],
            force_refresh=options.force_refresh,
        )

        print("\n[7/7] Building reconciliation plan...", flush=True)
        plan = plan_reconciliation(
            desired_users,
            zendesk_users,
            in_scope_entra_ids=in_scope_ids,
            suspend_when_out_of_scope=_behavior(config, "suspend_when_out_of_scope", True),
            suspend_when_entra_disabled=_behavior(config, "suspend_when_entra_disabled", True),
            protect_zendesk_staff_roles=_behavior(config, "protect_zendesk_staff_roles", True),
            resolutions=resolutions,
            allow_email_bootstrap=options.allow_email_bootstrap,
        )
        plan = add_user_field_actions(plan, desired_users, zendesk_users, field_keys=field_keys)
        plan.extend(membership_conflicts)
        plan.sort(key=lambda row: (str(row.get("action")), str(row.get("name") or "").lower()))
        counts = summarize_plan(plan)
        unresolved_conflicts = [row for row in plan if row.get("action") == "CONFLICT"]
        conflict_path = save_conflicts(unresolved_conflicts)

        bootstrap_candidates: list[dict] = []
        unresolved_bootstrap_reviews: list[dict] = []
        bootstrap_review_path = None
        if options.include_bootstrap_review:
            bootstrap_candidates = build_review_candidates(plan)
            bootstrap_review_path = save_review_candidates(bootstrap_candidates)
            unresolved_bootstrap_reviews = unresolved_review_candidates(bootstrap_candidates, bootstrap_decisions)

        print("      Reconciliation plan complete.")
        print(f"      {len(unresolved_conflicts)} unresolved conflict(s) saved to: {conflict_path}")
        if options.include_bootstrap_review and bootstrap_review_path is not None:
            print(f"      {len(bootstrap_candidates)} initial email/name review candidate(s) saved to: {bootstrap_review_path}")
            print(f"      {len(unresolved_bootstrap_reviews)} initial match review(s) still require approval.")

    except (
        BootstrapReviewError, CacheError, ConfigError, ConflictSnapshotError,
        GraphError, ResolutionError, UserFieldSetupError, ZendeskError, KeyError, ValueError,
    ) as exc:
        print(f"\nERROR: {exc}")
        return 1

    _print_summary(counts, len(plan))
    _print_details(plan)
    if unresolved_conflicts:
        print_attention(
            "\n!!! CONFLICT REVIEW REQUIRED !!!\n"
            "Run: python .\\setup\\resolve_conflicts.py\n"
            "Then run the same dry run again to apply those saved decisions to the plan."
        )
        print()
    if options.include_bootstrap_review and unresolved_bootstrap_reviews:
        print_attention(
            "\n!!! INITIAL MATCH REVIEW REQUIRED !!!\n"
            f"{len(unresolved_bootstrap_reviews)} email-matched user(s) have a different Zendesk name.\n"
            "Run: python .\\setup\\review_bootstrap_matches.py\n"
            "Then run the bootstrap dry run again to confirm those decisions."
        )
        print()

    print(f"{options.label} COMPLETE: no Zendesk user data was modified.")
    if options.include_bootstrap_review:
        if unresolved_bootstrap_reviews:
            print("Bootstrap is NOT ready for application because initial match reviews remain.")
        else:
            print("No unresolved initial email/name reviews remain.")
    if unresolved_conflicts:
        print("This plan is NOT ready for application because unresolved conflicts remain.")
    elif options.force_refresh:
        print("This run used a fresh live Zendesk snapshot.")
    elif zendesk_source == "cache":
        print("Zendesk comparison used the local snapshot shown above for faster repeat testing.")
    print(f"Only the explicit read-only Zendesk scope '{requested_scope}' was requested.")
    return 0
