# Deployment

## Subscriptions

| Scope | Subscription | Purpose |
|---|---|---|
| Backup | `<backup-subscription-id>` | Isolated backup platform |
| Source | `<source-subscription-id>` | Cosmos Table private endpoint and read-only role assignment |

Source account:

`/subscriptions/<source-subscription-id>/resourceGroups/rg-sintoken-backend-dev-plc/providers/Microsoft.DocumentDB/databaseAccounts/cdb-sintoken-backend-dev-plc`

## Required decisions

Before the first apply, approve the resource naming prefix/group, non-overlapping VNet and subnet CIDRs, tags, UTC schedule, alert action group/recipients, source RU budget and window, storage redundancy, and GitHub environment approvers. Parameters intentionally keep these choices explicit.

## Deployment order

1. Run `scripts/preflight.sh` with a read-only operator identity.
2. Validate and deploy the backup-subscription stack with its dedicated OIDC identity.
3. Build and push the application image to the development ACR.
4. Validate and deploy source integration with a separate source-subscription OIDC identity.
5. Confirm private endpoint approval and private DNS from the workload network.
6. Start a bounded manual backup job and complete the Phase 1 acceptance checks.
7. Run a manually authorized restore against the isolated target and verify count/type/hash evidence.
8. Enable monthly restore access and scheduling only after the restore gate passes.
9. Lock the storage immutability policy only after retention behavior is validated.

Do not combine source and backup deployment privileges. Runtime UAMIs must never be used by CI.

During development, ACR public network access is enabled so the OIDC-authenticated `ubuntu-latest` publisher can push images without static registry credentials. The private endpoint remains available for workload pulls. Before production acceptance, disable ACR public access and move publishing to a private-network runner or another approved private build path.

Store tenant IDs, subscription IDs, and full Azure resource IDs only as GitHub environment variables. The workflows require `AZURE_TENANT_ID`, `AZURE_BACKUP_SUBSCRIPTION_ID`, `AZURE_SOURCE_SUBSCRIPTION_ID`, and `SOURCE_COSMOS_ACCOUNT_RESOURCE_ID`; do not commit their values.

## Validation

```bash
az bicep restore --file infra/main.bicep
az bicep build --file infra/main.bicep
az deployment sub what-if \
  --location polandcentral \
  --template-file infra/main.bicep \
  --parameters infra/parameters/nonprod.bicepparam \
  --result-format FullResourcePayloads \
  --no-pretty-print > what-if.json
python3 scripts/guard-what-if.py what-if.json
```

The guard rejects deletes, replacements, VNet peering, public-access enablement, NSG relaxation, and unapproved role-assignment changes. Initial role assignments require an explicit reviewed override:

```bash
ALLOW_ROLE_ASSIGNMENT_CHANGES=1 python3 scripts/guard-what-if.py what-if.json
```

The override only acknowledges review; it does not alter the deployment.
