# Deployment and CI/CD

The repository uses secretless GitHub Actions authentication. Azure identifiers are GitHub environment variables, not repository content. Container images are promoted only by immutable `sha256` digest.

## Pipeline map

| Workflow | Trigger | Azure environment | Purpose |
|---|---|---|---|
| `validate.yml` | pull request, `main`, manual | none | Python quality/security tests, Bicep build, container build |
| `deploy-backup.yml` | manual | `backup-infrastructure` | Guarded backup-subscription what-if and optional apply |
| `deploy-source-integration.yml` | manual | `source-integration` | Guarded private-endpoint approval and source read-role deployment |
| `publish-image.yml` | `v*` tag, manual | `release` | Build, push, attest, record, and optionally deploy an immutable image |
| `deploy-image.yml` | manual | `release` | Promote or roll back both jobs to an existing digest |
| `codeql.yml` | repository security triggers | none | CodeQL scanning |

Deploy workflows use trusted refs and protected environments. Pull-request jobs never receive Azure tokens.

## One-time prerequisites

The operator running the bootstrap must be able to create managed identities, federated credentials, custom roles, and role assignments in both subscriptions, and must have GitHub repository administration access. Install and authenticate `az`, `gh`, and `jq` first.

Export environment-specific values only in the local shell:

```bash
export AZURE_BACKUP_SUBSCRIPTION_ID='<backup-subscription-id>'
export AZURE_SOURCE_SUBSCRIPTION_ID='<source-subscription-id>'
export SOURCE_COSMOS_ACCOUNT_RESOURCE_ID='/subscriptions/<source-subscription-id>/resourceGroups/<source-rg>/providers/Microsoft.DocumentDB/databaseAccounts/<source-account>'
export AZURE_RESOURCE_GROUP='<backup-resource-group>'
export BACKUP_PREFIX='<bicep-prefix>'
export AZURE_LOCATION='<azure-region>'
export GITHUB_REPOSITORY='<organization>/<repository>'
export DEPLOYMENT_BRANCH='main' # change when the default deployment branch differs
./scripts/bootstrap-github-oidc.sh
```

The idempotent script creates:

- an infrastructure deployment identity with Contributor and Role Based Access Control Administrator on the backup subscription;
- a release identity with `AcrPush` and a custom Container Apps job-image deployment role on the backup resource group;
- a source identity with a narrow custom integration role on the source resource group;
- one environment-scoped federated credential for each identity;
- the three GitHub environments and their non-secret variables; and
- deployment protection rules requiring the bootstrap operator's approval and allowing only `DEPLOYMENT_BRANCH` (plus `v*` tags for releases).

No client secret is created. Client IDs, tenant IDs, subscription IDs, and resource IDs are identifiers rather than credentials, but they remain outside this public repository.

The first run can occur before ACR exists. Run the script again after the initial infrastructure apply so it discovers ACR and sets `ACR_NAME` and `ACR_LOGIN_SERVER`. Set `CONFIGURE_GITHUB=false` to prepare Azure without modifying GitHub.

### GitHub environment variables

`backup-infrastructure`:

- `AZURE_TENANT_ID`
- `AZURE_BACKUP_SUBSCRIPTION_ID`
- `AZURE_BACKUP_DEPLOY_CLIENT_ID`
- `AZURE_LOCATION`
- `AZURE_RESOURCE_GROUP`
- `SOURCE_COSMOS_ACCOUNT_RESOURCE_ID`
- `BACKUP_JOB_NAME`
- `ACR_LOGIN_SERVER` after the first deployment

`source-integration`:

- `AZURE_TENANT_ID`
- `AZURE_SOURCE_SUBSCRIPTION_ID`
- `AZURE_SOURCE_DEPLOY_CLIENT_ID`
- `SOURCE_COSMOS_ACCOUNT_RESOURCE_ID`

`release`:

- `AZURE_TENANT_ID`
- `AZURE_BACKUP_SUBSCRIPTION_ID`
- `AZURE_RELEASE_CLIENT_ID`
- `AZURE_RESOURCE_GROUP`
- `ACR_NAME`
- `ACR_LOGIN_SERVER`
- `BACKUP_JOB_NAME`
- `RESTORE_JOB_NAME`

The bootstrap uses its authenticated GitHub operator as the initial required reviewer and permits self-approval so a single maintainer can operate nonproduction. It permits deployment workflows from the configured branch and immutable `v*` release tags. Before production use, replace that reviewer with the owning team, prevent self-review, add deployment wait timers if required, and keep environment administrators limited.

## Deployment order

1. Run `scripts/preflight.sh` with a read-only operator identity.
2. Run **Deploy backup platform** with `apply=false`; inspect the uploaded what-if artifact.
3. Re-run with `apply=true`, schedules disabled, and an empty image for the first bootstrap only.
4. Re-run `scripts/bootstrap-github-oidc.sh` to populate ACR variables.
5. Run **Release job image** manually. It builds Linux AMD64, publishes SBOM/provenance, records the digest, and updates both jobs. The restore job remains dormant.
6. Read `sourceIntegrationParameters` from the backup deployment artifact and run **Deploy source integration** first with `apply=false`, then with `apply=true` after review.
7. Confirm private endpoint approval and private DNS from the workload network.
8. Start one bounded manual backup and verify its encrypted manifest and exact `cards` exclusion.
9. Enable the daily schedule only after backup acceptance.

Restore execution, restore access, and restore scheduling remain disabled until a separately approved isolated-target test.

## Infrastructure changes

`deploy-backup.yml` resolves the image in this order: explicit workflow input, current live backup-job image, then the disabled bootstrap placeholder. This prevents an infrastructure-only deployment from reverting a released digest. Any nonempty image must match the configured ACR repository and a full SHA-256 digest.

Every run performs Bicep validation and a subscription what-if. `scripts/guard-what-if.py` rejects deletes, replacements, VNet peering, public-access enablement, NSG relaxation, and unexpected role changes. Initial reviewed role assignments are explicitly acknowledged by the workflow; environment approval is the human gate.

Source integration is resource-group-scoped. Its identity can deploy only the expected Cosmos account integration resources in the source resource group; it cannot deploy the backup platform.

## Build, release, promotion, and rollback

A `v*` tag runs the same immutable build as a manual release, deploys the digest after environment approval, and creates a GitHub release containing the digest. Manual releases can publish without deployment by clearing the `deploy` input.

To promote an already-published digest or roll back, run **Deploy job image** with the exact reference from a release or `image-reference-*` artifact:

```text
<registry>.azurecr.io/cosmos-table-backup@sha256:<64-hex-character-digest>
```

The workflow updates both backup and dormant restore jobs, reads their effective images back from Azure, and fails unless both exactly match. Tags are never accepted as deployment inputs.

## Forking to another organization or production

1. Fork the sanitized repository; do not import historical clones containing removed environment identifiers.
2. Create a production parameter file from `infra/parameters/nonprod.bicepparam`. Keep real identifiers out of it and override them from GitHub variables.
3. Choose new subscriptions, globally unique names, network ranges, retention, alert recipients, and tags.
4. Run the bootstrap for the new `<organization>/<repository>`. Federated subjects are repository- and environment-specific, so identities from the original repository cannot be reused.
5. Apply branch protection, protected release tags, CODEOWNERS, required CI, environment reviewers, and deployment wait timers before enabling delivery.
6. Use separate production identities and environments; never share nonproduction identities.
7. Disable ACR public network access. Run release jobs on an approved private-network runner or private build service that can reach the ACR private endpoint. Change `runs-on` accordingly before production acceptance.
8. Keep admin credentials, anonymous pull, shared keys, and static Azure credentials disabled.
9. Perform backup acceptance before enabling backup scheduling, then a separately approved isolated restore acceptance before enabling restore access or scheduling.

The workflow files deliberately contain no organization name. The bootstrap derives the repository with `gh repo view` unless `GITHUB_REPOSITORY` is supplied.

## Local validation

```bash
uv sync --frozen --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --cov=cosmos_table_backup --cov-fail-under=90
az bicep restore --file infra/main.bicep
az bicep build --file infra/main.bicep
bash -n scripts/*.sh
```

For a local what-if, supply all environment-specific parameters explicitly and run the same guard used by CI. Never save what-if or deployment output containing resource IDs in the repository.
