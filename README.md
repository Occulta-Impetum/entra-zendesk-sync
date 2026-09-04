# entra-zendesk-sync

Unattended Microsoft Entra to Zendesk user provisioning and organization synchronization using Microsoft Graph and Zendesk OAuth.

## Project goals

This project is intended to provide a reusable, self-hosted alternative to the built-in Microsoft Entra Zendesk provisioning connector. It is designed around unattended Microsoft Graph authentication, Zendesk OAuth, Entra group-based scope, and explicit Entra-group-to-Zendesk-organization mappings.

Key design principles:

- Safe by default: synchronization runs in dry-run mode unless `--apply` is explicitly supplied.
- Entra groups define provisioning scope. Groups may use static or dynamic membership.
- Selected Entra groups are mapped to Zendesk organizations during setup.
- Stable object IDs are stored in configuration; names are used only for human-readable setup and reporting.
- Existing Zendesk users can be adopted by email during initial setup and then linked to Entra using Zendesk `external_id`.
- After initial setup, operational identity matching uses the immutable Entra object ID only; email is not used to decide that two people are the same user.
- Users who leave provisioning scope should be suspended rather than deleted.
- Ambiguous matches and multiple mapped-group memberships are conflicts until an administrator explicitly resolves them.
- Slow or asynchronous operations should always provide visible progress output.
- Dry-run execution requests only read scopes. Write-capable scopes are reserved for explicit `--apply` execution.

## Repository structure

```text
entra-zendesk-sync/
├── sync.py
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
│
├── setup/
│   ├── configure.py
│   ├── resolve_conflicts.py
│   ├── review_bootstrap_matches.py
│   ├── create_certificate.ps1
│   ├── test_graph_auth.py
│   ├── test_group_discovery.py
│   └── test_zendesk_auth.py
│
├── lib/
│   ├── __init__.py
│   ├── bootstrap_review.py
│   ├── cache.py
│   ├── conflicts.py
│   ├── graph.py
│   ├── zendesk.py
│   ├── config.py
│   ├── reconcile.py
│   ├── resolutions.py
│   └── logging_utils.py
│
├── config/
│   └── config.example.yaml
│
├── cache/
│   └── .gitignore
│
├── tests/
│   └── __init__.py
│
└── logs/
    └── .gitignore
```

## Setup flow

The graphical setup wizard authenticates to both services, loads available Entra security groups and Zendesk organizations, lets the administrator select provisioning groups with checkboxes, maps each selected group to a Zendesk organization with dropdowns, and writes non-secret configuration to `config/config.yaml`.

Run from the repository root:

```powershell
python .\setup\configure.py
```

The group-selection page includes search/filtering, Select All Visible, Clear Visible, scrolling, and preservation of existing selections when the wizard is rerun. The mapping page provides one Zendesk organization dropdown per selected Entra group and restores existing mappings where possible.

Production secrets and machine-specific authentication material must not be committed to Git. Secrets remain in `.env`; tenant-specific configuration, cache data, logs, conflict decisions, and initial-match review decisions are excluded from Git.

## Authentication strategy

### Microsoft Graph

Microsoft Graph access uses unattended application authentication with an Entra app registration and certificate-based client credentials.

The Entra app registration currently needs Microsoft Graph application permissions `User.Read.All` and `GroupMember.Read.All`, both with tenant admin consent.

For Windows administrators, `setup/create_certificate.ps1` can create a self-signed RSA certificate, export the public `.cer` file for upload to Entra, and export a password-protected `.pfx` containing the private key for use by the sync runtime.

Run from PowerShell:

```powershell
.\setup\create_certificate.ps1
```

The script writes certificate files under `setup/certificates/` by default. Certificate and private-key files are excluded from Git. Upload only the `.cer` file to the Entra app registration. Keep the `.pfx` file and its password private.

Validate unattended authentication:

```powershell
python .\setup\test_graph_auth.py
python .\setup\test_group_discovery.py
```

### Zendesk

Zendesk access uses OAuth client credentials rather than deprecated API tokens. Setup/organization discovery uses `organizations:read`. Reconciliation explicitly requests `users:read` regardless of the default scope stored in `.env`.

Validate Zendesk OAuth and organization discovery with:

```powershell
python .\setup\test_zendesk_auth.py
```

`ZENDESK_OAUTH_SCOPE` is a fallback for callers that do not explicitly pass a scope. Runtime sync modes request their own least-privilege scope.

## Dry-run reconciliation

Run the normal read-only reconciliation with:

```powershell
python .\sync.py
```

The first run downloads the Zendesk user population and saves a local snapshot under `cache/`. Repeat dry runs reuse that snapshot to avoid repeatedly paging through the full Zendesk tenant.

Force a fresh Zendesk snapshot with:

```powershell
python .\sync.py --refresh-zendesk-cache
```

Normal dry runs are the initial/bootstrap workflow: they match `external_id` first and may use an exact email match to adopt an existing Zendesk user. After initial setup is complete, operational runs use `external_id: entra:<Entra object ID>` as the authoritative identity key and do not use email to decide whether an existing Zendesk profile is the same person.

A production-behavior preview can be run with:

```powershell
python .\sync.py --final-dry-run
```

The final dry run ignores the cached Zendesk snapshot, refreshes live state, and uses external-ID-only operational identity rules. Write execution is still disabled while the initial bootstrap plan is being validated.

Every run writes the full terminal output to a timestamped file under `logs/`.

## Conflict review

Unresolved reconciliation conflicts are written to `cache/conflicts.json`. Review them with:

```powershell
python .\setup\resolve_conflicts.py
```

The GUI displays the Entra user, desired group/organization, conflict reason, relevant Zendesk candidates, and a decision control. Supported decisions include choosing a mapped group, choosing a specific Zendesk candidate where safe, intentionally replacing a pre-existing Zendesk external ID during initial adoption, skipping the Entra user, or leaving the conflict unresolved. Decisions are persisted locally in `config/conflict_resolutions.yaml` and automatically applied to later dry runs.

## Initial email/name match review

During initial email-based adoption, an existing Zendesk user's name may differ from the Entra name. This is not automatically treated as proof of a different person: nicknames, preferred names, old names, spelling differences, and inconsistent historical entry are common.

The normal dry run writes email-matched `ADOPT`/`RELINK` rows that also contain `UPDATE NAME` to `cache/bootstrap_review.json`. Review them with:

```powershell
python .\setup\review_bootstrap_matches.py
```

The GUI shows the Entra/HR identity beside the existing Zendesk identity and lets the administrator either approve the existing Zendesk user while standardizing its name to the Entra/HR value, leave the item unresolved, or mark it for manual cleanup. The same decision can be applied in bulk to all reviews of the same type. Decisions are stored locally in `config/bootstrap_review_resolutions.yaml`.

For environments where Entra is populated from HR, the intended authoritative name is the HR-provided/legal name. The review exists because a name mismatch alone cannot reliably establish whether the Zendesk profile belongs to another person.

### Reused email address warning

Initial email matching is a migration convenience, not a permanent identity key. If an organization reuses email addresses, an initial email match can identify a Zendesk user that previously belonged to someone else. Approving that match keeps its historical Zendesk tickets and identity history.

After initial setup, the sync does not fall back to email matching. If an in-scope Entra object ID has no corresponding `entra:<object-id>` in Zendesk, operational reconciliation plans a new user rather than silently renaming an old profile that happens to have the same email address. Zendesk itself requires email identities to be unique, so a genuinely reused email that is still attached to an old Zendesk user may require manual Zendesk identity cleanup before a separate new user can be created.

## Generated configuration

The GUI writes configuration in this form:

```yaml
version: 1
entra:
  tenant_id: "..."
  client_id: "..."
zendesk:
  subdomain: "example"
  default_role: "end-user"
mappings:
  - entra_group:
      id: "..."
      name: "Example Zendesk Users"
    zendesk_organization:
      id: 123456789
      name: "Example Organization"
behavior:
  suspend_when_out_of_scope: true
  suspend_when_entra_disabled: true
  ambiguous_group_membership: conflict
  protect_zendesk_staff_roles: true
  dry_run_by_default: true
```

## Current status

Certificate authentication, Graph group/user discovery, Zendesk OAuth, graphical group-to-organization configuration, cached read-only reconciliation, conflict detection/resolution, and graphical initial email/name match review are implemented. Write execution remains intentionally disabled until the bootstrap plan and review decisions are validated.
