# Incident recovery — 2026-10-27

## Summary

Production incremental synchronization had stopped advancing after an unresolved
operational email collision on 2026-09-15. Because the baseline remained stale,
subsequent hourly runs accumulated a legitimate backlog until the Entra
change-volume guard blocked the run on 2026-10-27.

The incident also exposed a race between Zendesk SAML sign-in and hourly
provisioning: Zendesk can create an end-user from SAML before the provisioning
sync assigns the managed `external_id = entra:<Entra object ID>`. Those users
then appear as exact-email collisions with blank `external_id`.

## Root cause and behavior established

- Entra owns the current account/email assignment and authoritative user fields.
- Entra object ID owns person identity/history.
- Zendesk email ownership never transfers identity/history between Entra objects.
- A Zendesk end-user with blank `external_id`, exact current email, and a live
  matching Entra user may be explicitly adopted to that Entra object after
  safety validation.
- A Zendesk user already carrying a different managed
  `entra:<old-object-id>` must not be adopted to the new Entra identity.
- Staff identities remain protected.
- Normal scheduled runs continue to use the guarded incremental
  `python .\sync.py --apply` path.
- Recovery catch-up uses a fresh complete external-ID-only reconciliation so
  Zendesk drift accumulated during a stale baseline is not left behind.

## Recovery completed

The 10 SAML-created collision users were reviewed and assigned the correct
current Entra external IDs. A subsequent full reconciliation reported zero
unresolved conflicts.

A recovery full-reconcile dry run then reported:

- CREATE: 22
- UPDATE ORGANIZATION: 9
- UPDATE EMPLOYEE ID: 32
- UPDATE JOB TITLE: 35
- UPDATE MANAGER: 24
- CLEAR MANAGER: 16
- UNSUSPEND: 1
- SUSPEND: 27
- PROTECTED: 1
- NO CHANGE: 468
- PLAN ROWS: 548

The recovery apply completed successfully:

- Created: 22
- First-pass updated: 42
- Manager relationships updated: 40
- No-write/protected/skipped rows: 484
- External-ID verification: all 547 expected managed links present
- Operational Entra baseline saved atomically after successful writes

## Recovery tooling added

- `setup/recover_sso_adoptions.py`
  - previews and explicitly adopts only verified blank-external-ID SSO-created
    Zendesk end-users
  - never replaces a nonblank external ID
- `sync.py --catch-up-dry-run`
  - fresh complete external-ID-only recovery reconciliation
  - read-only
- `sync.py --catch-up-apply`
  - rebuilds the same complete live reconciliation immediately before writes
  - refuses unresolved conflicts and unexpected email-update identity changes
  - advances the incremental baseline only after successful write and manager
    verification

## Remaining hardening work

Recovery is complete, but the production design still needs follow-up hardening:

1. Safely handle blank-external-ID exact-email SSO-created end-users in normal
   operational synchronization.
2. Prevent one user-level conflict from blocking unrelated safe users.
3. Replace all-or-nothing baseline advancement with per-identity progress or an
   equivalent pending-state model.
4. Create/deduplicate actionable Zendesk incidents for sync conflicts and safety
   stops, with a non-Zendesk fallback if Zendesk itself is unavailable.
5. Review current Zendesk and Microsoft Entra SAML documentation and, if
   supported, send the immutable Entra object ID as Zendesk `external_id` in
   SAML to reduce the provisioning race.
6. Add operational monitoring for Scheduled Task nonzero exits.
7. Extend the conflict GUI to support the SSO-created blank-external-ID adoption
   case.
