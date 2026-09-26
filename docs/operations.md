# Operations

## Run contract

A successful run discovers current tables, excludes exact case-sensitive `cards`, exports every included table, uploads encrypted table objects, then writes the encrypted manifest. No manifest means the run is incomplete and must not be restored.

The export is a logical per-table scan; it is not a globally point-in-time-consistent snapshot across tables.

## Pre-deployment checks

```bash
./scripts/preflight.sh
```

Confirm naming prefix, resource group, VNet/subnet CIDRs, tags, daily UTC schedule, action group, source RU budget/window, storage redundancy, and GitHub environment approvers before applying a deployment.

## Phase 1 acceptance

1. Deploy without relaxing private networking or RBAC.
2. Verify private DNS from the Container Apps environment.
3. Run one manual backup.
4. Observe two consecutive scheduled backups.
5. Inject one controlled failed run and verify failure alerting.
6. Verify the 26-hour dead-man alert.
7. Review source request units, latency, and throttling during export.
8. Run the negative permission tests in `docs/security.md`.
9. Validate lifecycle behavior before locking immutability.

## Incident triage

- **No manifest:** treat the run as failed. Preserve partial blobs for investigation and allow lifecycle cleanup.
- **401/403 from Cosmos:** check the Table data-plane role assignment and managed-identity audience; never enable keys.
- **Name resolution failure:** check private DNS links and endpoint approval; never enable public access as a workaround.
- **429 from Cosmos:** reduce page size/concurrency or move the schedule; do not exceed the agreed source RU budget.
- **Key Vault wrap failure:** verify the versioned HSM key is enabled and the backup identity has only wrap permission.
- **Blob conflict:** use a new run ID. Never overwrite an existing immutable object.

Logs must not contain entity values, wrapped-key plaintext, tokens, or connection strings. Correlate by run ID, table name, object path, counts, durations, and sanitized Azure error codes.

## Phase 2 restore validation

Restore uses a separate dormant identity with unwrap, backup-read, and isolated-target-only write permissions. The restore CLI rejects source/target equality, incomplete runs, unauthenticated ciphertext, and missing explicit confirmation. It emits count and deterministic-hash evidence without entity values.

The monthly restore job and its access assignments are disabled by default. Enable `restoreAccessEnabled` and `restoreScheduleEnabled` together only after a manual isolated restore succeeds. After an access window, explicitly remove the conditional restore role assignments because an ordinary incremental ARM deployment does not delete assignments created by an earlier deployment; verify effective access is gone.
