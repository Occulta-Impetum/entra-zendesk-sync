# entra-zendesk-sync

Unattended Microsoft Entra to Zendesk user provisioning and organization synchronization using Microsoft Graph and Zendesk OAuth.

## Project goals

This project is intended to provide a reusable, self-hosted alternative to the built-in Microsoft Entra Zendesk provisioning connector. It is designed around unattended Microsoft Graph authentication, Zendesk OAuth, Entra group-based scope, and explicit Entra-group-to-Zendesk-organization mappings.

Key design principles:

- Safe by default: synchronization runs in dry-run mode unless `--apply` is explicitly supplied.
- Entra groups define provisioning scope. Groups may use static or dynamic membership.
- Selected Entra groups are mapped to Zendesk organizations during setup.
- Stable object IDs are stored in configuration; names are used only for human-readable setup and reporting.
- Existing Zendesk users can be adopted by email initially and then linked to Entra using Zendesk `external_id`.
- Users who leave provisioning scope should be suspended rather than deleted.
- Conflicting membership in multiple mapped groups should be reported rather than guessed.
- Slow or asynchronous operations should always provide visible progress output.

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
│   ├── create_certificate.ps1
│   ├── test_graph_auth.py
│   ├── test_group_discovery.py
│   └── test_zendesk_auth.py
│
├── lib/
│   ├── __init__.py
│   ├── graph.py
│   ├── zendesk.py
│   ├── config.py
│   ├── reconcile.py
│   └── logging_utils.py
│
├── config/
│   └── config.example.yaml
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

The group-selection page includes search/filtering, Select All Visible, Clear Visible, scrolling, and preservation of existing selections when the wizard is rerun. The mapping page provides one Zendesk organization dropdown per selected Entra group and restores existing mappings where possible. The wizard validates that every selected group has an organization before saving.

Production secrets and machine-specific authentication material must not be committed to Git. Secrets remain in `.env`; `config/config.yaml` stores immutable object IDs and human-readable names only.

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

Copy `.env.example` to `.env` and populate the Entra values, then validate unattended authentication:

```powershell
python .\setup\test_graph_auth.py
python .\setup\test_group_discovery.py
```

### Zendesk

Zendesk access uses OAuth client credentials rather than deprecated API tokens. The setup/discovery stage requires `organizations:read`.

Validate Zendesk OAuth and organization discovery with:

```powershell
python .\setup\test_zendesk_auth.py
```

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

Certificate authentication, Microsoft Graph user/group discovery, Zendesk OAuth organization discovery, and the graphical Entra-group-to-Zendesk-organization configuration wizard are implemented. The next major milestone is reconciliation and sync execution logic.
