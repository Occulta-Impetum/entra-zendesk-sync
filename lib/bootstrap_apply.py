"""Guarded one-time bootstrap write workflow for Entra -> Zendesk migration."""

from __future__ import annotations

from collections import Counter
from typing import Any

from lib.bootstrap_review import (
    BootstrapReviewError,
    build_review_candidates,
    load_review_decisions,
    save_review_candidates,
    unresolved_review_candidates,
)
from lib.cache import CacheError, save_zendesk_users_cache
from lib.config import ConfigError, load_config, validate_config
from lib.conflicts import ConflictSnapshotError, save_conflicts
from lib.graph import GraphError, get_graph_access_token, get_group_user_members, load_graph_config
from lib.reconcile import EXTERNAL_ID_PREFIX, build_desired_users, plan_reconciliation, summarize_plan
from lib.resolutions import ResolutionError, load_resolutions
from lib.zendesk import (
    ZendeskError,
    create_user,
    get_access_token,
    get_users,
    load_zendesk_config,
    update_user,
)

ZENDESK_APPLY_SCOPE = "users:read users:write"


class BootstrapApplyError(RuntimeError):
    """Raised when bootstrap apply cannot proceed safely."""


def _behavior(config: dict[str, Any], name: str, default: bool) -> bool:
    behavior = config.get("behavior") or {}
    return bool(behavior.get(name, default))


def _action_parts(row: dict[str, Any]) -> set[str]:
    return {
        part.strip()
        for part in str(row.get("action") or "").split("+")
        if part.strip()
    }


def _print_summary(counts: Counter[str], total_rows: int) -> None:
    print("\nBootstrap apply plan")
    print("--------------------")
    preferred = [
        "CREATE",
        "ADOPT",
        "RELINK",
        "UPDATE EMAIL",
        "UPDATE NAME",
        "UPDATE ORGANIZATION",
        "UNSUSPEND",
        "SUSPEND",
        "SKIP",
        "PROTECTED",
        "CONFLICT",
        "NO CHANGE",
    ]
    shown: set[str] = set()
    for action in preferred:
        if counts.get(action):
            print(f"{action:<22} {counts[action]:>5}")
            shown.add(action)
    for action in sorted(set(counts) - shown):
        print(f"{action:<22} {counts[action]:>5}")
    print(f"{'PLAN ROWS':<22} {total_rows:>5}")


def _build_live_bootstrap_plan() -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    set[str],
    dict[str, str],
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Re-read Entra and live Zendesk, then rebuild the approved bootstrap plan."""
    print("\n[1/6] Loading configuration and saved review decisions...", flush=True)
    config = load_config()
    validate_config(config)
    mappings = list(config["mappings"])
    resolutions = load_resolutions()
    bootstrap_decisions = load_review_decisions()
    print(f"      Configuration valid. {len(mappings)} group mapping(s) loaded.")
    print(f"      {len(resolutions)} saved conflict decision(s) loaded.")
    print(f"      {len(bootstrap_decisions)} saved initial-match review decision(s) loaded.")
    print("      Identity mode: INITIAL SETUP (external_id first, then exact email bootstrap).")

    print("\n[2/6] Authenticating to Microsoft Graph...", flush=True)
    graph_config = load_graph_config()
    graph_token = get_graph_access_token(graph_config)
    print("      Microsoft Graph authentication successful.")

    print("\n[3/6] Reading configured Entra group memberships...", flush=True)
    group_members: list[tuple[dict, list[dict]]] = []
    for index, mapping in enumerate(mappings, start=1):
        entra_group = mapping.get("entra_group") or {}
        group_id = str(entra_group.get("id") or "")
        group_name = str(entra_group.get("name") or group_id)
        print(
            f"      [{index}/{len(mappings)}] {group_name}: reading direct user members...",
            flush=True,
        )
        members = get_group_user_members(graph_token, group_id)
        print(f"            {len(members)} user member(s) found.")
        group_members.append((mapping, members))

    desired_users, membership_conflicts, in_scope_ids = build_desired_users(
        group_members,
        resolutions=resolutions,
    )
    unresolved_memberships = sum(
        1 for row in membership_conflicts if row.get("action") == "CONFLICT"
    )
    print(
        f"      {len(in_scope_ids)} unique Entra user(s) in scope; "
        f"{unresolved_memberships} unresolved multiple-group conflict(s)."
    )

    print("\n[4/6] Authenticating to Zendesk with write-capable user scope...", flush=True)
    zendesk_config = load_zendesk_config()
    print(f"      Requested scope: {ZENDESK_APPLY_SCOPE}")
    zendesk_token, token_data = get_access_token(
        zendesk_config,
        scope=ZENDESK_APPLY_SCOPE,
    )
    granted_scope = token_data.get("scope") or token_data.get("scopes") or "not reported"
    print(f"      Zendesk authentication successful. Granted scope: {granted_scope}")

    print("\n[5/6] Re-reading live Zendesk users immediately before apply...", flush=True)
    zendesk_users = get_users(zendesk_token, zendesk_config["subdomain"])
    cache_path = save_zendesk_users_cache(zendesk_users, subdomain=zendesk_config["subdomain"])
    print(f"      {len(zendesk_users)} Zendesk user(s) loaded from live Zendesk.")
    print(f"      Local snapshot refreshed: {cache_path}")

    print("\n[6/6] Rebuilding bootstrap plan from live state...", flush=True)
    plan = plan_reconciliation(
        desired_users,
        zendesk_users,
        in_scope_entra_ids=in_scope_ids,
        suspend_when_out_of_scope=_behavior(config, "suspend_when_out_of_scope", True),
        suspend_when_entra_disabled=_behavior(config, "suspend_when_entra_disabled", True),
        protect_zendesk_staff_roles=_behavior(config, "protect_zendesk_staff_roles", True),
        resolutions=resolutions,
        allow_email_bootstrap=True,
    )
    plan.extend(membership_conflicts)
    plan.sort(key=lambda row: (str(row.get("action")), str(row.get("name") or "").lower()))

    unresolved_conflicts = [row for row in plan if row.get("action") == "CONFLICT"]
    conflict_path = save_conflicts(unresolved_conflicts)
    bootstrap_candidates = build_review_candidates(plan)
    review_path = save_review_candidates(bootstrap_candidates)
    unresolved_reviews = unresolved_review_candidates(bootstrap_candidates, bootstrap_decisions)

    print(f"      {len(unresolved_conflicts)} unresolved conflict(s) saved to: {conflict_path}")
    print(f"      {len(bootstrap_candidates)} initial email/name review candidate(s) saved to: {review_path}")
    print(f"      {len(unresolved_reviews)} initial match review(s) still require approval.")

    return (
        config,
        plan,
        in_scope_ids,
        zendesk_config,
        zendesk_token,
        unresolved_conflicts,
        unresolved_reviews,
    )


def _validate_apply_plan(
    plan: list[dict[str, Any]],
    unresolved_conflicts: list[dict[str, Any]],
    unresolved_reviews: list[dict[str, Any]],
) -> None:
    if unresolved_conflicts:
        raise BootstrapApplyError(
            f"Refusing bootstrap apply: {len(unresolved_conflicts)} unresolved conflict(s) remain."
        )
    if unresolved_reviews:
        raise BootstrapApplyError(
            f"Refusing bootstrap apply: {len(unresolved_reviews)} initial match review(s) remain."
        )

    email_updates = [row for row in plan if "UPDATE EMAIL" in _action_parts(row)]
    if email_updates:
        raise BootstrapApplyError(
            "Refusing bootstrap apply because the live plan contains UPDATE EMAIL actions. "
            "Zendesk primary-email changes require the User Identities API and different OAuth scope handling; "
            "that path is intentionally not enabled in bootstrap apply yet."
        )

    invalid_creates = [
        row
        for row in plan
        if "CREATE" in _action_parts(row)
        and (not str(row.get("email") or "").strip() or not str(row.get("name") or "").strip())
    ]
    if invalid_creates:
        raise BootstrapApplyError(
            f"Refusing bootstrap apply: {len(invalid_creates)} CREATE row(s) are missing name or email."
        )


def _confirm_apply(counts: Counter[str], total_rows: int) -> bool:
    _print_summary(counts, total_rows)
    print(
        "\nThis is the one-time bootstrap migration. It will create and modify Zendesk end users.\n"
        "Existing email-matched users will keep their Zendesk ticket history.\n"
        "Type APPLY exactly to continue, or press Enter to cancel."
    )
    try:
        return input("Confirmation: ").strip() == "APPLY"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _write_row(
    row: dict[str, Any],
    *,
    access_token: str,
    subdomain: str,
) -> str:
    actions = _action_parts(row)
    if not actions or actions <= {"NO CHANGE", "SKIP", "PROTECTED"}:
        return "SKIPPED"

    entra_id = str(row.get("entra_id") or "").strip()
    external_id = f"{EXTERNAL_ID_PREFIX}{entra_id}"

    if "CREATE" in actions:
        create_user(
            access_token,
            subdomain,
            name=str(row.get("name") or "").strip(),
            email=str(row.get("email") or "").strip(),
            external_id=external_id,
            organization_id=int(row["zendesk_org_id"]),
        )
        return "CREATED"

    zendesk_id = row.get("zendesk_id")
    if zendesk_id is None:
        raise BootstrapApplyError(
            f"Plan row for {row.get('name') or entra_id} requires an update but has no Zendesk user id."
        )

    fields: dict[str, Any] = {}
    if "ADOPT" in actions or "RELINK" in actions:
        fields["external_id"] = external_id
    if "UPDATE NAME" in actions:
        fields["name"] = str(row.get("name") or "").strip()
    if "UPDATE ORGANIZATION" in actions:
        fields["organization_id"] = int(row["zendesk_org_id"])
    if "SUSPEND" in actions:
        fields["suspended"] = True
    if "UNSUSPEND" in actions:
        fields["suspended"] = False

    if fields:
        update_user(
            access_token,
            subdomain,
            int(zendesk_id),
            fields=fields,
        )
        return "UPDATED"
    return "SKIPPED"


def _verify_after_apply(
    *,
    config: dict[str, Any],
    in_scope_ids: set[str],
    access_token: str,
    subdomain: str,
) -> tuple[int, int]:
    """Refresh live Zendesk and verify every in-scope Entra id now resolves by external_id."""
    print("\nPost-apply verification: refreshing Zendesk users...", flush=True)
    users = get_users(access_token, subdomain)
    save_zendesk_users_cache(users, subdomain=subdomain)
    external_ids = {
        str(user.get("external_id") or "").strip().lower()
        for user in users
        if user.get("external_id")
    }
    missing = [
        entra_id
        for entra_id in sorted(in_scope_ids)
        if f"{EXTERNAL_ID_PREFIX}{entra_id}".lower() not in external_ids
    ]
    if missing:
        print(f"      WARNING: {len(missing)} in-scope Entra user(s) still lack their expected external_id.")
    else:
        print(f"      Verified external_id linkage for all {len(in_scope_ids)} in-scope Entra user(s).")
    return len(in_scope_ids), len(missing)


def run_bootstrap_apply() -> int:
    """Execute the guarded one-time bootstrap migration."""
    print("Entra -> Zendesk Sync (BOOTSTRAP APPLY)")
    print("=======================================")

    try:
        (
            config,
            plan,
            in_scope_ids,
            zendesk_config,
            zendesk_token,
            unresolved_conflicts,
            unresolved_reviews,
        ) = _build_live_bootstrap_plan()

        _validate_apply_plan(plan, unresolved_conflicts, unresolved_reviews)
        counts = summarize_plan(plan)
        if not _confirm_apply(counts, len(plan)):
            print("\nBootstrap apply cancelled. No Zendesk data was modified.")
            return 2

        actionable = [
            row
            for row in plan
            if not _action_parts(row) <= {"NO CHANGE", "SKIP", "PROTECTED"}
        ]
        print(f"\nApplying {len(actionable)} actionable row(s)...", flush=True)
        created = 0
        updated = 0
        skipped = 0

        for index, row in enumerate(plan, start=1):
            actions = _action_parts(row)
            if not actions or actions <= {"NO CHANGE", "SKIP", "PROTECTED"}:
                skipped += 1
                continue
            name = str(row.get("name") or row.get("email") or row.get("entra_id") or "-")
            print(
                f"      [{index}/{len(plan)}] {row.get('action')}: {name}...",
                flush=True,
            )
            result = _write_row(
                row,
                access_token=zendesk_token,
                subdomain=zendesk_config["subdomain"],
            )
            if result == "CREATED":
                created += 1
            elif result == "UPDATED":
                updated += 1
            else:
                skipped += 1

        print("\nBootstrap writes completed.")
        print(f"      Created: {created}")
        print(f"      Updated: {updated}")
        print(f"      No-write/protected/skipped rows: {skipped}")

        total_in_scope, missing_links = _verify_after_apply(
            config=config,
            in_scope_ids=in_scope_ids,
            access_token=zendesk_token,
            subdomain=zendesk_config["subdomain"],
        )
        if missing_links:
            print(
                "\nBOOTSTRAP APPLY FINISHED WITH VERIFICATION WARNINGS. "
                f"{missing_links} of {total_in_scope} in-scope Entra users are not linked by external_id."
            )
            return 1

        print("\nBOOTSTRAP APPLY COMPLETE.")
        print("All in-scope Entra users are now linked by immutable entra:<object-id> external IDs.")
        print("The next validation step is the operational root sync.py, which does not match identities by email.")
        return 0

    except (
        BootstrapApplyError,
        BootstrapReviewError,
        CacheError,
        ConfigError,
        ConflictSnapshotError,
        GraphError,
        ResolutionError,
        ZendeskError,
        KeyError,
        ValueError,
    ) as exc:
        print(f"\nERROR: {exc}")
        print("Bootstrap apply stopped. Re-running it is safe: the live plan is rebuilt before every attempt.")
        return 1
