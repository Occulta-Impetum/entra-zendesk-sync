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

## Planned setup flow

The setup wizard will guide the administrator through:

1. Microsoft Graph authentication validation.
2. Entra group discovery and selection.
3. Zendesk OAuth authentication validation.
4. Zendesk organization discovery.
5. Entra group to Zendesk organization mapping.
6. Provisioning behavior choices.
7. Saving non-secret configuration to `config/config.yaml`.

Production secrets and machine-specific authentication material must not be committed to Git.

## Authentication strategy

### Microsoft Graph

Production use will prefer unattended application authentication with an Entra app registration and certificate-based client credentials.

For Windows administrators, `setup/create_certificate.ps1` can create a self-signed RSA certificate, export the public `.cer` file for upload to Entra, and export a password-protected `.pfx` containing the private key for use by the sync runtime.

Run from PowerShell:

```powershell
.\setup\create_certificate.ps1
```

The script writes certificate files under `setup/certificates/` by default. Certificate and private-key files are excluded from Git. Upload only the `.cer` file to the Entra app registration. Keep the `.pfx` file and its password private.

### Zendesk

Zendesk access will use OAuth rather than deprecated Zendesk API tokens.

## Current status

Repository scaffolding and the Windows certificate setup helper are complete. The next implementation milestone is read-only unattended Microsoft Graph authentication and group discovery.
