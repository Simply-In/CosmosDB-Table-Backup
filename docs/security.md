# Security invariants

> **Navigation:** [Business overview](../README.md) · [Deployment](deployment.md) · [Operations](operations.md) · [Backup format](backup-format.md) · [Infrastructure](../infra/README.md)
>
> This document is authoritative for release-blocking security and trust-boundary invariants.

The deployment must fail closed. These invariants are release blockers:

1. Runtime authentication uses managed identities only. Cosmos keys, storage keys, SAS tokens, client secrets, and credentials in settings are forbidden.
2. The source Cosmos account is read through its Table private endpoint. Blob and Key Vault are Private Link-only. During development, ACR additionally permits public OIDC-authenticated publishing while retaining its private endpoint for workload pulls; public access must be disabled before production acceptance.
3. The backup VNet has no peering to production or hub networks and no route through production firewalls or NAT gateways.
4. Public network and shared-key access stay disabled where supported, except for the documented development-stage ACR publishing path. Key Vault permits its `AzureServices` firewall bypass solely because ARM key lifecycle deployment is otherwise rejected; the public endpoint remains disabled and runtime access remains RBAC-scoped.
5. `cards` is rejected during discovery before a table client is opened. Discovery failure and zero included tables fail the run.
6. Each run gets a new 256-bit DEK. Every encrypted object and retry gets a unique random 96-bit AES-GCM nonce.
7. The backup identity may wrap but never unwrap the DEK. The exact versioned HSM KEK identifier is recorded.
8. An encrypted manifest is committed last and is the sole completion marker. Partial objects never represent a successful run.
9. Runtime identities have no control-plane Owner, Contributor, User Access Administrator, deployment, or role-assignment permissions.
10. Backup blobs cannot be overwritten or deleted by the runtime identity. Immutability is locked only after nonproduction validation.

## Required negative tests

- Cosmos entity create, update, and delete are denied.
- Blob overwrite and delete are denied.
- Key Vault unwrap and key-management operations are denied.
- Public DNS or public data-plane access cannot bypass Private Link.
- A run with only excluded tables fails.

GitHub Actions deployment identities are separate from runtime identities and scoped to their respective subscriptions. Production/source integration remains an independently reviewed deployment.

## Governed restore lifecycle and data-only trust boundary

**Architecture change:** Cosmos DB for Table can deny table metadata operations even when the runtime has native Table Contributor. This is not permission to grant a runtime control-plane access, use keys, enable public access, or write the source. In governed data-only restore, an independently authorized operator/GitHub Actions identity manages **only the isolated target's table lifecycle**. The restore runtime remains managed-identity-only, performs entity reads and create-only inserts, and never invokes Table service list/delete/create or ARM. The compatibility/default mode retains its existing native Table lifecycle behavior.

`restore-test --plan` authenticates the committed bootstrap, encrypted manifest, and **every encrypted table object** with the configured managed identity before emitting any private JSON. No target client is constructed by this command. The plan is capped at 16 KiB and 100 tables and carries the selected backup UUID, configured source/target and backup-store bindings, manifest plaintext SHA-256, and sorted table names. Its private stdout uses bounded `restore.plan.part` base64 frames and a digest-bound `restore.plan.complete` marker (see the [format contract](backup-format.md#governed-data-only-mode-unchanged-backup-wire-format)); frames bypass the logger and are not telemetry. This is an explicit exception permitting table names only in the private operator control channel, never entity keys/values. Planning does not configure the normal logger or telemetry exporter; only safe failure events go to stderr. All restore commands suppress Azure SDK logging, including HTTP INFO logs, while data-only execution preserves application allowlisted telemetry. These names and the plan are **private control artifacts**, not telemetry: never echo, upload to public Actions artifacts, or include in job summaries. The source binding is the configured isolation guard, not cryptographic evidence of source provenance: v1 manifests do not contain a source resource ID.

For execution, `RESTORE_DATA_ONLY=true` requires a pinned `RESTORE_BACKUP_ID` and `RESTORE_PREPARATION_JSON` containing exactly that plan. The governed stage supplies this assertion only after independently validating the binding, exclusively locking the target for the whole run, preparing exactly the authenticated table set, and confirming it is empty. The assertion is a trusted operator input, **not a signed receipt or a control-plane proof**. Do not allow untrusted users to replace it or change job overrides. Runtime reauthenticates the manifest, compares every plan field (including store and manifest digest), reads every expected table to prove initial emptiness before any inserts, and authenticates all encrypted table objects against pinned ETags before the first entity mutation. Missing/unreadable/nonempty tables, unexpected binding or conflicting inserts fail closed. Runtime cannot observe an unexpected extra table without the forbidden metadata enumeration; the governed stage must prove the exact table set before and after data verification.

Data-only report v2 therefore has `status=data_verified_pending_table_set` and `table_set_verified=false`; it emits `restore.data_verified`, never `restore.completed`. Entity count, key hash and content hash verification remain mandatory. A zero process exit proves only data verification, **not successful end-to-end restore**. The workflow may declare completion only after its ARM postvalidation proves the exact authenticated table set and binds the runtime report to the plan. Do not weaken the existing `restore.completed` success contract. The operator must discard the preparation assertion and reprepare the empty target on retry; partial target contents are never overwritten by data-only runtime. Separate lifecycle credentials must never be passed to the container, and the operator must have no source write role.
