# Phase 1 backup and Phase 2 restore-validation infrastructure

> **Navigation:** [Business overview](../README.md) · [Deployment guide](../docs/deployment.md) · [Operations runbooks](../docs/operations.md) · [Backup format](../docs/backup-format.md) · [Security invariants](../docs/security.md)

This directory composes the isolated backup subscription, dormant-by-default restore validation, and the independently authorized source-subscription integration. All reusable resource types use explicitly pinned Azure Verified Modules (AVM); native Bicep is limited to custom least-privilege role definitions, Cosmos private-endpoint approval/data-plane assignment, and Azure Monitor scheduled-query alerts where no suitable AVM resource module exists.

## Deployment order

1. Run tenant/subscription preflight checks and register `Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.DocumentDB`, `Microsoft.Insights`, `Microsoft.KeyVault`, `Microsoft.ManagedIdentity`, `Microsoft.Network`, `Microsoft.OperationalInsights`, and `Microsoft.Storage` in the applicable subscription.
2. Deploy `main.bicep` to target subscription `<backup-subscription-id>`. Keep `scheduleEnabled=false`, `restoreScheduleEnabled=false`, and `restoreAccessEnabled=false` for bootstrap. The deployment identity needs resource deployment and role-definition/role-assignment permissions. Review `what-if` first.
3. Publish the application image to the created Premium ACR and use an immutable `@sha256:` reference.
4. From the `main.bicep` outputs, pass the UAMI principal ID and private endpoint values to `deployment/production-integration.bicep`. Deploy that resource-group-scoped template separately under the source-subscription OIDC identity in the source account's resource group.
5. Validate DNS and the bounded read-only Cosmos smoke test. Validate Blob create plus overwrite/delete denial and Key Vault wrap plus unwrap denial. Only then redeploy with the immutable image and `scheduleEnabled=true`.
6. Validate an isolated restore through the [governed on-demand workflow](../docs/operations.md#run-a-governed-on-demand-backup-and-restore-validation): review and apply a redeployment with `scheduleEnabled=false`, `restoreAccessEnabled=true`, and `restoreScheduleEnabled=false` (protected workflow inputs `enable_schedule=false`, `enable_restore_access=true`, `enable_restore_validation=false`). This temporarily disables the backup schedule enabled in step 5; verify both jobs are Manual before acceptance, then restore the previously accepted backup schedule through reviewed deployment after the acceptance window. Require successful UUID-pinned data verification plus the workflow's independent exact final ARM table-set verification and passed aggregate artifact; `restore.data_verified` alone is not completion.
7. Governed on-demand success does not establish legacy direct/monthly Table lifecycle feasibility. Only after a separate successful legacy lifecycle test and operational approval, enable monthly restore validation through a change that sets **both** `restoreAccessEnabled=true` and `restoreScheduleEnabled=true`. The job remains manual unless both flags are true. If continuous monthly validation is required, the security owner must explicitly accept that restore-only grants remain active. To end an access window, set both flags false **and remove the four restore role assignments explicitly**, or manage this template with an Azure Deployment Stack configured to delete resources that become unmanaged. Ordinary incremental ARM deployments do not delete role assignments omitted by a false condition; verify effective access before declaring the identity dormant.
8. Lock the storage immutability policy only after the explicit operational approval and nonprod WORM tests. The template intentionally creates an **unlocked** 7-day time-based policy; locking is irreversible and is an operator gate.

Example backup-domain commands:

```sh
az bicep restore --file infra/main.bicep
az bicep build --file infra/main.bicep
az deployment sub what-if \
  --subscription <backup-subscription-id> \
  --location polandcentral \
  --template-file infra/main.bicep \
  --parameters infra/parameters/nonprod.bicepparam \
  --parameters backupImage='<registry>.azurecr.io/cosmos-table-backup@sha256:<digest>'
```

Example source integration (values are outputs from step 2):

```sh
az deployment group what-if \
  --subscription <source-subscription-id> \
  --resource-group <source-resource-group> \
  --template-file infra/deployment/production-integration.bicep \
  --parameters sourceCosmosAccountResourceId='<source-id>' \
               backupIdentityPrincipalId='<principal-id>' \
               cosmosPrivateEndpointName='<pe-name>' \
               cosmosPrivateEndpointResourceId='<pe-id>'
```

### Phase 1 backup container contract

The backup ACA job supplies the application configuration names exactly as consumed by `BackupConfig`:

- `COSMOS_TABLE_ENDPOINT` is derived from the account-name segment of `sourceCosmosAccountResourceId` as `https://<account>.table.cosmos.azure.com`. Private DNS resolves that service hostname to the approved source private endpoint.
- `BACKUP_STORAGE_ACCOUNT_URL` is the HTTPS Blob service URL.
- `BACKUP_CONTAINER_NAME` is the immutable backup container name.
- `BACKUP_KEY_ID` comes from the Key Vault AVM key output `uriWithVersion`; it is an exact `.../keys/<name>/<version>` URI rather than an unversioned alias.
- `EXCLUDED_TABLES_JSON` is the JSON serialization of the `excludedTables` array, for example `["cards"]`. Matching is exact and case-sensitive.
- `AZURE_CLIENT_ID` selects the backup UAMI. Application Insights variables retain their existing AAD-authenticated values.

The backup job's fixed `replicaTimeout` is 7500 seconds: 7200 seconds of work + the governed-only 180-second console hold + 120 seconds of process/startup/flush margin. This replaces the previous 7200-second job ceiling for both manual and scheduled backups; ordinary executions still have no hold. Retry limit 1 and single-replica execution are unchanged. Restore retains its 14400-second ceiling and retry limit 0. Redeploy through the approved infrastructure workflow before governed execution; the supervisor rejects a stale or altered backup timeout rather than overriding persistent configuration. [Microsoft's job schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/2024-03-01/jobs#jobconfiguration) defines `configuration.replicaTimeout` as a required integer maximum in seconds; it does not publish a numeric upper bound. The [Start API](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/jobs/start) accepts only main/init container overrides, not `replicaTimeout`. We use a concrete finite 7500-second bound, not a claimed undocumented service maximum or an execution-level timeout override. See [operations](../docs/operations.md) for supervisor and workflow margins; offline validation does not claim deployed behavior.

Key rotation creates a new version, but an existing ACA job revision remains pinned to the version emitted by its deployment. Redeploy the template after an approved rotation to create a job revision using the new exact key URI. The selected version is also returned as `backupKeyVersionUri`.

Explicitly activate the monthly restore test only after the governed manual gate, a separate successful legacy Table lifecycle feasibility test, and operational approval:

```sh
az deployment sub what-if \
  --subscription <backup-subscription-id> \
  --location polandcentral \
  --template-file infra/main.bicep \
  --parameters infra/parameters/nonprod.bicepparam \
  --parameters backupImage='<registry>.azurecr.io/cosmos-table-backup@sha256:<digest>' \
               restoreAccessEnabled=true restoreScheduleEnabled=true
# Replace what-if with create only after approval.
```

## Phase 2 restore boundary

- On the initial deployment, the restore UAMI is created with `restoreAccessEnabled=false`, receives the always-present ACR pull needed by its dormant job but no Blob, Key Vault, monitoring-publish, or Cosmos data-plane assignments, and is tagged dormant. After conditional access has ever been enabled, a normal incremental deployment does not delete omitted assignments; use explicit RBAC removal or Deployment Stack unmanage deletion as described above.
- When activated, it receives Blob Data Reader only on the backup container, a custom key-metadata/unwrap-only role only on the versioned HSM key, ACR pull, telemetry publishing, and Cosmos built-in Data Contributor only on the generated restore-test account. It receives no source-account permission.
- The backup UAMI is unchanged: it cannot read backup blobs, unwrap keys, or write to the restore account.
- The restore target is a dedicated serverless Cosmos DB for Table account with public networking, local/key authentication (`disableLocalAuthentication=true`), and key-based metadata writes (`disableKeyBasedMetadataWriteAccess=true`) disabled. Its Table endpoint uses a private endpoint and the isolated VNet's private DNS zone. The metadata flag is defense in depth for account-key-authenticated writes, not a fix for Entra-authenticated Table lifecycle denials; see the [retired hypothesis and accepted nonproduction rollout](../docs/operations.md#understand-the-target-before-enabling-access).
- The restore account name is deterministically generated from the target subscription/resource group. The deployment explicitly fails if its resource ID equals `sourceCosmosAccountResourceId`; Cosmos global name uniqueness provides another collision guard. The application also receives both resource IDs and `RESTORE_REQUIRE_ISOLATED_TARGET=true` and must refuse equality before writing.
- The restore job uses the same immutable image digest as backup and defaults to `python -m cosmos_table_backup.cli restore-test`. Legacy direct/monthly mode emits `restore.completed` only after integrity and native table-set checks. The governed on-demand workflow overrides the container for authenticated planning and data-only execution: the operator manages isolated target tables through ARM, while runtime verifies entities and emits `restore.data_verified` with table-set verification pending. Only independent final ARM verification and a passed aggregate workflow artifact establish governed completion. Restore runtime failures emit `restore.failed`; supervisor preflight or postvalidation failures instead produce a failed aggregate report and nonzero exit and need not emit that runtime event, including failures after `restore.data_verified`. Governed success does not repair the legacy lifecycle.
- Restore-test data cleanup and account teardown are operator-owned. Do not repoint or reuse the isolated target for production. Deleting the target also removes its target-scoped data-plane assignment; disable restore access and schedule first.

## Security invariants

- No VNet peering, hub/firewall route, NAT gateway, public application ingress, or public protected data-plane access is created.
- Blob, Key Vault, ACR, source Cosmos Table, and restore Cosmos Table traffic uses private endpoints and private DNS zones linked only to the backup VNet.
- The ACA subnet NSG allows private endpoint HTTPS, Azure DNS, Entra ID, Azure Monitor, and the ACA control plane, then explicitly denies Internet and all remaining outbound traffic. Revalidate current regional ACA service-tag/platform dependencies before deployment; do not replace these rules with broad `AzureCloud` or Internet allows.
- Storage shared keys/public blob access and Key Vault/ACR public access are disabled. The runtime gets ACR pull, Blob add/write without read/delete, key metadata/wrap without unwrap, and Cosmos built-in Data Reader only.
- Version-level immutability is enabled at container creation. The write DataAction cannot distinguish create from overwrite; immutable object versions are the platform control that prevents mutation, and negative tests are mandatory before policy lock.
- Lifecycle deletion after day 14 is asynchronous and cannot remove an object still protected by WORM.

## Deployment prerequisites and platform gates

- Confirm Poland Central availability/quota for zone-redundant Container Apps workload-profile environments, Premium ACR, Premium Key Vault HSM keys, private endpoints, and the selected Storage redundancy. Set `zoneRedundant=false` only through an approved design change if regional support is unavailable.
- CIDRs must not overlap each other, Docker/platform reserved ranges, or organizational networks. The ACA delegated subnet should remain at least `/23` for workload profiles.
- Private endpoint approval resource names can be provider-generated. If Cosmos reports a connection name different from the PE name, query the source account's private endpoint connections and pass that connection name (the template parameter is named `cosmosPrivateEndpointName` for the common case).
- The source deployment principal needs Cosmos role-assignment and private-endpoint-approval rights but no write access to source entities. The backup deployment principal requires custom-role creation and RBAC assignment rights.
- Azure Monitor ingestion remains a narrowly allowed platform egress path. A fully private monitoring path requires Azure Monitor Private Link Scope plus private DNS and is not included because deny-by-default ACA control-plane bootstrap must first be proven in the target tenant/region.
- Alerts depend on the application emitting `backup.failed`, `backup.completed`, `restore.failed`, and `restore.completed` to workspace-backed Application Insights. Completion events must be emitted only after commit/integrity validation. The restore failure alert always exists; the 35-day restore success dead-man alert is created only while both restore access and its schedule are enabled. Tune/validate table names and alert queries against actual telemetry before enabling either schedule.
- Azure policies may require diagnostic categories, locks, private monitoring, or additional tags. Resolve policy findings in `what-if` before deployment.

## AVM pins verified against the public registry

Versions were checked against `mcr.microsoft.com/v2/bicep/avm/res/.../tags/list`: virtual network `0.10.2`, NSG `0.5.3`, private DNS `0.8.1`, private endpoint `0.12.1`, UAMI `0.6.0`, Log Analytics `0.16.1`, Application Insights `0.8.0`, Storage `0.33.1`, Key Vault `0.14.2`, ACR `0.13.1`, Container Apps managed environment `0.16.0`, Container Apps job `0.7.2`, and Cosmos DB account `0.21.1`.
