# entra-zendesk-sync

Unattended Microsoft Entra to Zendesk user provisioning and organization synchronization using Microsoft Graph and Zendesk OAuth.

## Project goals

This project is a reusable, self-hosted alternative to the built-in Microsoft Entra Zendesk provisioning connector. It is designed around unattended Microsoft Graph authentication, Zendesk OAuth, Entra group-based scope, explicit Entra-group-to-Zendesk-organization mappings, and Entra as the authoritative source of user data.

Key design principles:

- Safe by default: synchronization runs in dry-run mode unless `--apply` is explicitly supplied.
- Entra groups define provisioning scope and map to Zendesk organizations.
- Stable object IDs are stored in configuration; names are only for readability.
- Initial bootstrap may adopt an existing Zendesk user by exact email, then writes `external_id: entra:<Entra object ID>`.
- Operational synchronization never uses email to decide that two people are the same identity.
- Entra/HR values are authoritative for name, employee ID, job title, manager, enabled state, and organization mapping.
- Users who leave provisioning scope are suspended rather than deleted.
- Ambiguous identity or group cases become conflicts rather than guesses.
- Slow operations always display progress.
- Dry runs explicitly request read-only scopes; write scopes are reserved for apply paths.

## Repository structure

```text
entra-zendesk-sync/
├── sync.py                         # small operational/scheduled entrypoint
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── setup/
│   ├── bootstrap_sync.py           # one-time migration/bootstrap
│   ├── configure.py
│   ├── resolve_conflicts.py
│   ├── review_bootstrap_matches.py
│   ├── check_user_fields.py        # optional diagnostic only
│   ├── create_certificate.ps1
│   ├── test_graph_auth.py
│   ├── test_group_discovery.py
│   └── test_zendesk_auth.py
├── lib/
│   ├── runtime.py                  # full reconciliation runtime
│   ├── operational.py              # incremental operational planner
│   ├── bootstrap_apply.py
│   ├── bootstrap_review.py
│   ├── cache.py
│   ├── conflicts.py
│   ├── graph.py
│   ├── zendesk.py
│   ├── user_fields.py
│   ├── config.py
│   ├── reconcile.py
│   ├── resolutions.py
│   └── logging_utils.py
├── config/
│   └── config.example.yaml
├── cache/
│   └── .gitignore
├── tests/
└── logs/
    └── .gitignore
```

The production Scheduled Task should call the small root `sync.py`. Bootstrap-specific matching and migration review live under `setup/` and are not part of the normal scheduled command.

## Setup

Run the graphical configuration wizard from the repository root:

```powershell
python .\setup\configure.py
```

The wizard authenticates to Entra and Zendesk, discovers security groups and organizations, lets the administrator map groups to Zendesk organizations, and writes non-secret configuration to `config/config.yaml`.

Production secrets remain in `.env`. Certificates, production configuration, caches, logs, and review decisions are excluded from Git.

## Authentication

### Microsoft Graph

Microsoft Graph uses unattended certificate-based client credentials. The app registration needs application permissions:

- `User.Read.All`
- `GroupMember.Read.All`

Both require tenant admin consent.

### Zendesk

Zendesk uses OAuth client credentials rather than API tokens. Runtime code requests exact scopes for each operation instead of blindly using the `.env` default.

OAuth access tokens are cached locally by exact scope set until shortly before expiration. Tokens for different scope sets are never interchanged.

## Managed Zendesk fields

Bootstrap discovers and validates the Zendesk user-field schema. The sync manages:

- Zendesk standard `name`
- Zendesk standard `email` on create
- Zendesk `external_id` as `entra:<Entra object ID>`
- Zendesk organization from the mapped Entra security group
- Employee ID in a text user field, default key `employee_id`
- Job Title in `standard::job_title`
- Manager in `standard::manager`, a Zendesk user lookup relationship

The Employee ID field is created automatically during bootstrap apply if it does not already exist. Standard Job Title and Manager fields must already exist with the expected Zendesk types.

Manager writes happen after identities are established so the lookup stores the actual target Zendesk user ID.

## Initial bootstrap workflow

Dry run:

```powershell
python .\setup\bootstrap_sync.py
```

Final live preview:

```powershell
python .\setup\bootstrap_sync.py --final-dry-run
```

Apply:

```powershell
python .\setup\bootstrap_sync.py --apply
```

Bootstrap identity matching is intentionally different from scheduled operation:

1. exact `external_id` first
2. exact email fallback only during bootstrap
3. reviewed email matches can be adopted/relinked
4. unresolved or ambiguous cases block apply

Bootstrap apply rebuilds its plan from live Entra and Zendesk state, checks required fields, creates Employee ID if needed, requires the administrator to type `APPLY`, performs identity/organization/employee/title writes, then performs manager relationship writes in a second pass and verifies expected Entra external IDs.

A successful bootstrap also seeds the local Entra operational baseline so the first scheduled run does not need to rediscover unchanged Zendesk profiles.

## Conflict and initial-match review

Unresolved identity/group conflicts are written to `cache/conflicts.json` and reviewed with:

```powershell
python .\setup\resolve_conflicts.py
```

Initial email-matched users whose Zendesk name differs from the authoritative Entra/HR name are reviewed with:

```powershell
python .\setup\review_bootstrap_matches.py
```

A name difference by itself is not treated as proof of a different person.

## Operational / scheduled synchronization

The normal production entrypoint is:

```powershell
python .\sync.py
```

Normal operation is incremental. The sync:

1. reads the complete current in-scope Entra state
2. includes employee ID, job title, manager, enabled state, and desired organization
3. compares that authoritative state to `cache/entra_users.json`
4. queries Zendesk only for new, changed, or removed Entra identities
5. uses `external_id: entra:<object-id>` as the identity key
6. suspends identities removed from provisioning scope
7. saves the new Entra baseline only after a future successful operational apply

The Entra cache retains historical identity records rather than discarding them. That history is needed to safely recognize reused email addresses after a terminated account has disappeared from Entra.

### Reused email addresses

Operational sync does not adopt by email. If a new Entra object has no matching Zendesk external ID but its desired email is already in use, the sync only treats it as automatic email reuse when it can prove all of the following:

- the current email owner has a different `entra:<old-object-id>`
- the old Entra object ID is no longer present in the current authoritative snapshot
- the retained Entra history contains that old object ID
- the old user has an Employee ID
- the generated historical alias is not already in use

The planned repair is:

```text
jsmith@company.com
employee ID 123456
       ↓
old Zendesk user: jsmith123456@company.com
new Zendesk user: jsmith@company.com
```

The old Zendesk user is preserved, including its historical tickets. Only its primary email identity is renamed. The new Entra identity is then created as a separate Zendesk user.

Zendesk primary-email replacement uses the User Identities API, not the normal Users API. Zendesk documentation currently states that the User Identities API does not support resource-scoped `users:read/users:write`; therefore the eventual email-reuse apply path must request the broader identity-capable OAuth scope only when that repair is actually required. Normal incremental runs should not request that broad scope.

Any email collision that cannot be proven to be a retired managed identity becomes a conflict rather than being modified automatically.

## Full reconciliation

Normal scheduled runs do not download every Zendesk user. If an administrator intentionally wants to overwrite manual Zendesk drift and force all managed profiles back to Entra values, use:

```powershell
python .\sync.py --full-reconcile
```

`--refresh-zendesk-cache` remains as a deprecated alias for `--full-reconcile`.

A full reconciliation downloads a fresh complete Zendesk snapshot and compares all managed identities. This is intentionally heavier than normal scheduled operation.

## Operational apply status

Operational `--apply` remains intentionally disabled while the new incremental planner, targeted Zendesk lookup, historical Entra cache, and reused-email detection are validated in dry-run mode.

When enabled, operational apply will:

- re-read fresh Entra state
- execute only the targeted change set
- use write-capable user scopes only for normal user changes
- request identity-capable broad scope only when an email-reuse repair requires the User Identities API
- verify each collision repair before creating the replacement user
- save the new Entra cache only after the run completes successfully

Every run writes terminal output to a timestamped file under `logs/`.
