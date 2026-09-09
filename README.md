# entra-zendesk-sync

Unattended Microsoft Entra to Zendesk user provisioning and organization synchronization using Microsoft Graph and Zendesk OAuth.

## Project goals

This project is a reusable, self-hosted alternative to the built-in Microsoft Entra Zendesk provisioning connector. It is designed around unattended Microsoft Graph authentication, Zendesk OAuth client credentials, Entra group-based scope, explicit Entra-group-to-Zendesk-organization mappings, and Entra as the authoritative source of user data.

Key design principles:

- Safe by default: synchronization runs in dry-run mode unless `--apply` is explicitly supplied.
- Entra groups define provisioning scope and map to Zendesk organizations.
- Stable object IDs are stored in configuration; names are only for readability.
- Initial bootstrap may adopt an existing Zendesk user by exact email, then writes `external_id: entra:<Entra object ID>`.
- Operational synchronization never uses email to decide that two people are the same identity.
- Entra/HR values are authoritative for name, employee ID, job title, manager, enabled state, and organization mapping.
- Users who leave provisioning scope are suspended rather than deleted.
- Ambiguous identity or group cases become conflicts rather than guesses.
- Zendesk agent/admin identities are protected from sync changes.
- Slow operations always display progress.
- Dry runs explicitly request read-only scopes; write scopes are reserved for apply paths.
- Normal scheduled runs are incremental and query Zendesk only for identities whose authoritative Entra state changed.

## Repository structure

```text
entra-zendesk-sync/
├── sync.py                         # operational/scheduled entrypoint
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── setup/
│   ├── bootstrap_sync.py           # one-time migration/bootstrap
│   ├── configure.py                # graphical group/org mapping wizard
│   ├── resolve_conflicts.py
│   ├── review_bootstrap_matches.py
│   ├── check_user_fields.py        # optional diagnostic
│   ├── create_certificate.ps1
│   ├── test_graph_auth.py
│   ├── test_group_discovery.py
│   └── test_zendesk_auth.py
├── lib/
│   ├── runtime.py                  # full reconciliation runtime
│   ├── operational.py              # incremental operational planner
│   ├── operational_apply.py        # guarded incremental write engine
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

# First-time deployment

The intended deployment target is a trusted Windows machine/server that can securely hold the Entra certificate private key, Zendesk OAuth client secret, local caches, and logs.

These instructions intentionally assume a bare Windows Server. Do not assume Git, Python, the repository, a virtual environment, certificates, OAuth clients, configuration, or local cache state already exists.

## 0. Install required software

Install the following before cloning the repository:

- **Git for Windows** — required for the initial clone and future `git pull` updates. Git is not required by the Scheduled Task at runtime.
- **64-bit Python 3.10 or newer** — the codebase uses Python 3.10+ language features. A currently supported 64-bit Python release is recommended.
- **Windows PowerShell 5.1 or newer** — used by the certificate helper and deployment commands. Standard Windows Server Desktop Experience installations normally already include Windows PowerShell.

When installing Python with the standard Windows installer on a production server:

- **install Python for all users** so the runtime is machine-wide rather than tied to an administrator's personal Windows profile; this avoids making the Scheduled Task dependent on a path under `C:\Users\<username>\AppData\Local\Programs\Python\...`
- add Python to `PATH` so the initial setup commands are easy to run
- keep the standard Tcl/Tk component enabled because `setup/configure.py` uses Tkinter for the graphical configuration wizard
- ensure `pip` is installed

After installation, open a **new PowerShell window** so updated `PATH` values are loaded, then verify:

```powershell
git --version
python --version
python -m pip --version
where.exe python
```

Do not continue until all four commands succeed. For a production server, confirm the selected `python.exe` is the machine-wide installation and is **not** under a specific administrator's user profile.

> If the server is Windows Server Core without a graphical desktop/Tkinter capability, the current graphical `setup/configure.py` wizard cannot be used directly there. Run first-time configuration from a Windows machine with GUI support and securely transfer the resulting machine-specific configuration/state as appropriate, or add a non-GUI configuration path before deploying to Server Core.

## 1. Clone the repository

Choose the permanent application folder first. For example:

```powershell
New-Item -ItemType Directory -Path C:\SysadminBot -Force | Out-Null
Set-Location C:\SysadminBot
git clone https://github.com/Occulta-Impetum/entra-zendesk-sync.git EntraZendeskSync
Set-Location C:\SysadminBot\EntraZendeskSync
git status
```

`git status` should show a clean working tree.

For a long-running server deployment, use a normal local application path such as `C:\SysadminBot\EntraZendeskSync` or `C:\Apps\entra-zendesk-sync` rather than a OneDrive-synchronized working tree.

## 2. Create a Python virtual environment

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Validate the code before configuration:

```powershell
python -m unittest discover -s tests
```

## 3. Create the Microsoft Entra certificate

A helper script is included:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup\create_certificate.ps1
```

It creates:

- `entra-zendesk-sync.cer` — public certificate uploaded to the Entra app registration
- `entra-zendesk-sync.pfx` — password-protected certificate/private-key bundle kept only on the trusted runtime machine

The helper defaults to a two-year certificate and refuses to overwrite an existing certificate pair.

Upload only the `.cer` file to the Entra app registration. Never upload or commit the `.pfx` file.

## 4. Create/configure the Microsoft Entra app registration

Create a single-tenant app registration for the sync and upload the public `.cer` certificate under **Certificates & secrets > Certificates**.

Add these **Microsoft Graph application permissions**:

- `User.Read.All`
- `GroupMember.Read.All`

Grant tenant admin consent for both permissions.

The sync uses application/client-credential authentication; it does not require an interactive signed-in user during scheduled operation.

Record:

- Directory (tenant) ID
- Application (client) ID

## 5. Create/configure the Zendesk OAuth client

Create a Zendesk OAuth client as a **Confidential** client for this server-side integration. The client-credentials flow does not require a redirect URL.

The OAuth client should be created/owned by a Zendesk administrator because the resulting client-credentials tokens operate with that associated user's Zendesk permissions in addition to their OAuth scopes.

If **Allowed scopes** are configured on the client, allow the complete ceiling the sync may legitimately request:

- `organizations:read`
- `users:read`
- `users:write`
- `account_settings:read`
- `account_settings:write`
- broad `read`
- broad `write`

The broad `read write` pair is reserved for the Zendesk User Identities API during the uncommon reused-email repair path. Normal discovery, dry-run, and user-update operations request narrower resource scopes. `impersonate` is not required.

Record:

- Zendesk subdomain
- OAuth client identifier
- OAuth client secret

## 6. Create `.env`

Copy the template:

```powershell
Copy-Item .env.example .env
```

Populate:

```dotenv
ENTRA_TENANT_ID=<tenant-guid>
ENTRA_CLIENT_ID=<application-guid>
ENTRA_CERTIFICATE_PATH=C:\SysadminBot\EntraZendeskSync\certificates\entra-zendesk-sync.pfx
ENTRA_CERTIFICATE_PASSWORD=<pfx-password>

ZENDESK_SUBDOMAIN=<subdomain>
ZENDESK_OAUTH_CLIENT_ID=<oauth-client-id>
ZENDESK_OAUTH_CLIENT_SECRET=<oauth-client-secret>
ZENDESK_OAUTH_SCOPE=organizations:read
```

`ZENDESK_OAUTH_SCOPE` is only the default/fallback scope. Runtime code explicitly requests the exact scopes required for each operation.

Protect `.env` and the `.pfx` with NTFS permissions so only administrators and the Scheduled Task service account can read them. Neither file belongs in Git.

## 7. Validate authentication

From the activated virtual environment:

```powershell
python .\setup\test_graph_auth.py
python .\setup\test_group_discovery.py
python .\setup\test_zendesk_auth.py
```

Resolve authentication/permission errors before continuing.

## 8. Run the graphical configuration wizard

```powershell
python .\setup\configure.py
```

The wizard:

1. authenticates to Microsoft Graph
2. enumerates Entra security groups
3. authenticates to Zendesk
4. enumerates Zendesk organizations
5. lets the administrator choose in-scope groups
6. maps each selected Entra group to one Zendesk organization
7. stores immutable IDs and readable names in `config/config.yaml`

Production secrets remain in `.env`. `config/config.yaml`, certificates, caches, logs, and review decisions are excluded from Git.

### Migrating an existing production installation to a new host

If the Zendesk tenant has already been bootstrapped and operational synchronization is already in use, **do not rerun bootstrap simply because the runtime is moving to another machine**. Preserve the existing production identity state instead.

If the application lives under a broader automation root such as `C:\SysadminBot`, the simplest migration is usually to copy that entire tree to the new server. That preserves the application files and the machine-specific state together, including `.env`, certificates, `config/config.yaml`, `cache/entra_users.json`, and any review/conflict decisions. Copying the whole tree is preferred over manually picking individual state files when the parent automation folder is already the canonical home for the installation.

Treat the copied tree as **application data, configuration, and operational state**, not as a complete replacement for rebuilding the machine runtime. On the new host:

1. install machine-wide Python and Git
2. copy the existing automation tree, for example `C:\SysadminBot`
3. for each Python project, recreate its virtual environment and reinstall dependencies from `requirements.txt`; do not rely on a copied `.venv` because virtual environments can contain machine/interpreter-specific paths
4. update any machine-specific paths in `.env`, especially `ENTRA_CERTIFICATE_PATH`
5. recreate Scheduled Tasks rather than assuming they moved with the folder
6. reapply/verify NTFS permissions for `.env`, private certificates, `cache\`, and `logs\`
7. validate authentication on the new host
8. run a normal incremental **dry run**
9. run a read-only `--full-reconcile`
10. only then enable scheduled `--apply`

The production state that must survive the migration includes:

- `.env`
- the existing `.pfx` certificate/private-key bundle and, optionally, its `.cer` public certificate
- `config/config.yaml`
- `cache/entra_users.json`
- any existing conflict/review resolution files under `config\` or `cache\`

`cache/entra_users.json` is especially important. It is not disposable runtime cache: it is both the authoritative incremental comparison baseline and retained identity history used by the reused-email safety checks. Starting an already-bootstrapped production tenant on a new host without that file can make the installation behave like it has no operational history.

Cached OAuth access tokens do not need to be preserved; the new host can request fresh tokens as needed. `cache/zendesk_users.json` and old logs are also optional because full reconciliation can rebuild the Zendesk snapshot and new runs will create fresh logs.

Do not run `setup/bootstrap_sync.py --apply` during a host-only migration.

## 9. Run the initial bootstrap

This step is for a genuinely new deployment that has not already bootstrapped its Zendesk tenant. If you are moving an existing production installation to another host, follow the migration subsection above and skip this step.

Start with the normal bootstrap dry run:

```powershell
python .\setup\bootstrap_sync.py
```

Bootstrap identity matching is intentionally different from scheduled operation:

1. exact `external_id` first
2. exact email fallback only during bootstrap
3. reviewed email matches can be adopted/relinked
4. unresolved or ambiguous cases block apply

Review unresolved conflicts with:

```powershell
python .\setup\resolve_conflicts.py
```

Review initial exact-email matches whose Zendesk name differs from authoritative Entra/HR name with:

```powershell
python .\setup\review_bootstrap_matches.py
```

A name difference by itself is not treated as proof of a different person.

After reviews are complete, run the final live preview:

```powershell
python .\setup\bootstrap_sync.py --final-dry-run
```

Then apply:

```powershell
python .\setup\bootstrap_sync.py --apply
```

Bootstrap apply rebuilds its plan from live Entra and Zendesk state, checks required fields, creates Employee ID if needed, requires the administrator to type `APPLY`, performs identity/organization/employee/title writes, then performs manager relationship writes in a second pass and verifies expected Entra external IDs.

A successful bootstrap seeds the local Entra operational baseline so the first scheduled run does not need to rediscover unchanged Zendesk profiles.

## 10. Validate operational synchronization

Normal incremental dry run:

```powershell
python .\sync.py
```

A no-change run should collect the authoritative Entra snapshot, report zero changes, and skip Zendesk authentication/lookups entirely.

A guarded operational apply is enabled with:

```powershell
python .\sync.py --apply
```

Operational apply always rebuilds a fresh plan before writing. It does not apply a previously saved dry-run plan.

After the first live apply, validate the complete managed population with a read-only full reconcile:

```powershell
python .\sync.py --full-reconcile
```

A healthy post-bootstrap/post-apply full reconcile should contain only expected `NO CHANGE` and `PROTECTED` rows unless authoritative Entra changes occurred after the last apply.

## 11. Configure the Windows Scheduled Task

Run the root entrypoint with `--apply`; do not schedule bootstrap scripts.

Recommended action shape:

```text
Program/script:
C:\SysadminBot\EntraZendeskSync\.venv\Scripts\python.exe

Arguments:
C:\SysadminBot\EntraZendeskSync\sync.py --apply

Start in:
C:\SysadminBot\EntraZendeskSync
```

Use a dedicated service account or other controlled identity with:

- read/execute access to the repository and virtual environment
- read access to `.env` and the PFX
- modify access to `cache\` and `logs\`
- no unnecessary interactive privileges

Choose a schedule appropriate for how quickly Entra lifecycle changes should reach Zendesk. Standard recurring schedules should not overlap; configure the task not to start a second instance while a previous run is still active.

Run the task manually once after creation and inspect its timestamped log before relying on the schedule.

# Authentication details

## Microsoft Graph

Microsoft Graph uses unattended certificate-based client credentials with application permissions:

- `User.Read.All`
- `GroupMember.Read.All`

Both require tenant admin consent.

## Zendesk

Zendesk uses OAuth client credentials rather than API tokens. Runtime code requests exact scopes for each operation instead of blindly using the `.env` default.

OAuth access tokens are cached locally by exact scope set until shortly before expiration. Tokens for different scope sets are never interchanged. Token-cache writes are atomic, and an API request that receives HTTP 401 refreshes the same exact scope set and retries once.

# Managed Zendesk fields

Bootstrap discovers and validates the Zendesk user-field schema. The sync manages:

- Zendesk standard `name`
- Zendesk standard `email`
- Zendesk `external_id` as `entra:<Entra object ID>`
- Zendesk organization from the mapped Entra security group
- Employee ID in a text user field, default key `employee_id`
- Job Title in `standard::job_title`
- Manager in `standard::manager`, a Zendesk user lookup relationship

The Employee ID field is created automatically during bootstrap apply if it does not already exist. Standard Job Title and Manager fields must already exist with the expected Zendesk types.

Manager writes happen after identities are established so the lookup stores the actual target Zendesk user ID. Manager targets may be outside the provisioning groups; full reconciliation resolves them against the complete Zendesk snapshot by Entra external ID, with a unique exact-email fallback used only for relationship resolution.

# Operational / scheduled synchronization

Normal production dry run:

```powershell
python .\sync.py
```

Normal production apply:

```powershell
python .\sync.py --apply
```

Normal operation is incremental. The sync:

1. reads the complete current in-scope Entra state
2. includes employee ID, job title, manager, enabled state, and desired organization
3. compares that authoritative state to `cache/entra_users.json`
4. applies a configurable change-volume safety guard before Zendesk is touched
5. queries Zendesk only for new, changed, or removed Entra identities
6. uses `external_id: entra:<object-id>` as the identity key
7. suspends identities removed from provisioning scope
8. performs manager writes in a second pass after identity changes
9. saves the new Entra baseline atomically only after the complete apply succeeds

Default change-volume guard settings are documented in `config/config.example.yaml` and currently stop a run when a sufficiently large established baseline exceeds either absolute or percentage removal/change thresholds.

The Entra cache retains historical identity records rather than discarding them. That history is needed to safely recognize reused email addresses after a terminated account has disappeared from Entra.

## Reused email addresses

Operational sync does not adopt by email. If a new Entra object has no matching Zendesk external ID but its desired email is already in use, the sync only treats it as automatic email reuse when it can prove the old Zendesk owner is a retired managed identity. Safety checks include:

- the current email owner has a different `entra:<old-object-id>`
- the old Entra object ID is no longer present in the current authoritative snapshot
- retained Entra history contains that old object ID and Employee ID
- the old Zendesk user is still a suspended managed end-user immediately before repair
- the generated historical alias is not already in use
- the desired email is still owned exclusively by the expected retired Zendesk user immediately before repair

The planned repair is:

```text
jsmith@company.com
employee ID 123456
       ↓
old Zendesk user: jsmith123456@company.com
new Zendesk user: jsmith@company.com
```

The old Zendesk user is preserved, including its historical tickets. Its existing Entra external ID is never transferred to the replacement identity. Only its primary email identity is renamed. The new Entra identity is then created as a separate Zendesk user.

Zendesk primary-email replacement uses the User Identities API. The implementation requests broader `read write` scope only when an actual email-reuse repair requires that endpoint. Normal incremental runs do not request broad scope.

Any email collision that cannot be proven safe becomes a conflict rather than being modified automatically. If the collision owner is a Zendesk agent/admin, it is protected rather than adopted or duplicated.

# Full reconciliation

Normal scheduled runs do not download every Zendesk user. To intentionally compare the entire managed population to live Zendesk state, use:

```powershell
python .\sync.py --full-reconcile
```

`--refresh-zendesk-cache` remains as a deprecated alias for `--full-reconcile`.

Full reconciliation is read-only. It downloads a fresh complete Zendesk snapshot and compares all managed identities to authoritative Entra state. Operational identity ownership remains external-ID-only; email is consulted only for collision/protection checks and manager relationship resolution.

# Logging and local state

Every run writes terminal output to a timestamped file under `logs\`.

Important local state includes:

- `config/config.yaml` — non-secret immutable ID mappings and behavior settings
- `.env` — secrets and machine-specific authentication values
- `cache/entra_users.json` — authoritative incremental baseline plus retained identity history
- `cache/zendesk_users.json` — full-reconcile/bootstrap snapshot cache
- conflict/review decision files under `cache\`
- cached Zendesk OAuth access tokens

Cache writes that establish operational state are atomic. Do not delete `cache/entra_users.json` casually on a production deployment; it is both the incremental comparison baseline and retained evidence used for safe email-reuse handling.
