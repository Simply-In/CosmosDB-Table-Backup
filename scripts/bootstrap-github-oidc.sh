#!/usr/bin/env bash
set -euo pipefail

: "${AZURE_BACKUP_SUBSCRIPTION_ID:?Set the backup subscription ID}"
: "${AZURE_SOURCE_SUBSCRIPTION_ID:?Set the source subscription ID}"
: "${SOURCE_COSMOS_ACCOUNT_RESOURCE_ID:?Set the full source Cosmos DB account resource ID}"
: "${AZURE_RESOURCE_GROUP:?Set the backup platform resource group name}"
: "${BACKUP_PREFIX:?Set the deployed backup resource prefix}"

GITHUB_REPOSITORY=${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}
GITHUB_OIDC_SUBJECT_PREFIX=${GITHUB_OIDC_SUBJECT_PREFIX:-$(gh api "repos/$GITHUB_REPOSITORY/actions/oidc/customization/sub" --jq '.sub_claim_prefix // empty' 2>/dev/null || true)}
GITHUB_OIDC_SUBJECT_PREFIX=${GITHUB_OIDC_SUBJECT_PREFIX:-repo:$GITHUB_REPOSITORY}
AZURE_LOCATION=${AZURE_LOCATION:-polandcentral}
CONFIGURE_GITHUB=${CONFIGURE_GITHUB:-true}
OIDC_RESOURCE_GROUP=${OIDC_RESOURCE_GROUP:-rg-${BACKUP_PREFIX}-github-oidc}
BACKUP_ENVIRONMENT=${BACKUP_ENVIRONMENT:-backup-infrastructure}
SOURCE_ENVIRONMENT=${SOURCE_ENVIRONMENT:-source-integration}
RELEASE_ENVIRONMENT=${RELEASE_ENVIRONMENT:-release}
DEPLOYMENT_BRANCH=${DEPLOYMENT_BRANCH:-main}
BACKUP_IDENTITY_NAME=${BACKUP_IDENTITY_NAME:-${BACKUP_PREFIX}-github-infra}
SOURCE_IDENTITY_NAME=${SOURCE_IDENTITY_NAME:-${BACKUP_PREFIX}-github-source}
RELEASE_IDENTITY_NAME=${RELEASE_IDENTITY_NAME:-${BACKUP_PREFIX}-github-release}
BACKUP_JOB_NAME=${BACKUP_JOB_NAME:-${BACKUP_PREFIX}-daily-backup}
RESTORE_JOB_NAME=${RESTORE_JOB_NAME:-${BACKUP_PREFIX}-monthly-restore-test}

source_subscription=$(cut -d/ -f3 <<<"$SOURCE_COSMOS_ACCOUNT_RESOURCE_ID")
source_resource_group=$(cut -d/ -f5 <<<"$SOURCE_COSMOS_ACCOUNT_RESOURCE_ID")
source_provider=$(cut -d/ -f7 <<<"$SOURCE_COSMOS_ACCOUNT_RESOURCE_ID")
source_type=$(cut -d/ -f8 <<<"$SOURCE_COSMOS_ACCOUNT_RESOURCE_ID")
source_account=$(cut -d/ -f9 <<<"$SOURCE_COSMOS_ACCOUNT_RESOURCE_ID")
source_provider_lower=$(tr '[:upper:]' '[:lower:]' <<<"$source_provider")
if [[ "$source_subscription" != "$AZURE_SOURCE_SUBSCRIPTION_ID" || "$source_provider_lower" != "microsoft.documentdb" \
  || "$source_type" != "databaseAccounts" || -z "$source_resource_group" || -z "$source_account" ]]; then
  echo 'SOURCE_COSMOS_ACCOUNT_RESOURCE_ID is invalid or belongs to another source subscription.' >&2
  exit 1
fi

for command in az gh jq; do
  command -v "$command" >/dev/null || { echo "$command is required" >&2; exit 1; }
done
az account show >/dev/null
if [[ "$CONFIGURE_GITHUB" == "true" ]]; then
  gh auth status >/dev/null
fi

backup_tenant=$(az account show --subscription "$AZURE_BACKUP_SUBSCRIPTION_ID" --query tenantId --output tsv)
source_tenant=$(az account show --subscription "$AZURE_SOURCE_SUBSCRIPTION_ID" --query tenantId --output tsv)

ensure_group() {
  local subscription=$1 group=$2
  az group create --subscription "$subscription" --name "$group" --location "$AZURE_LOCATION" --output none
}

ensure_identity() {
  local subscription=$1 group=$2 name=$3
  if ! az identity show --subscription "$subscription" --resource-group "$group" --name "$name" --output none 2>/dev/null; then
    az identity create --subscription "$subscription" --resource-group "$group" --name "$name" --output none
  fi
}

ensure_federation() {
  local subscription=$1 group=$2 identity=$3 credential=$4 environment=$5 expected actual
  expected="${GITHUB_OIDC_SUBJECT_PREFIX}:environment:${environment}"
  actual=$(az identity federated-credential show --subscription "$subscription" --resource-group "$group" \
    --identity-name "$identity" --name "$credential" --query subject --output tsv 2>/dev/null || true)
  if [[ -z "$actual" ]]; then
    az identity federated-credential create --subscription "$subscription" --resource-group "$group" \
      --identity-name "$identity" --name "$credential" \
      --issuer https://token.actions.githubusercontent.com --subject "$expected" \
      --audiences api://AzureADTokenExchange --output none
  elif [[ "$actual" != "$expected" ]]; then
    az identity federated-credential update --subscription "$subscription" --resource-group "$group" \
      --identity-name "$identity" --name "$credential" --subject "$expected" --output none
  fi
}

ensure_role_assignment() {
  local subscription=$1 scope=$2 principal=$3 role=$4
  if [[ $(az role assignment list --subscription "$subscription" --scope "$scope" \
    --assignee-object-id "$principal" --role "$role" --query 'length(@)' --output tsv) == 0 ]]; then
    az role assignment create --subscription "$subscription" --scope "$scope" \
      --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
      --role "$role" --output none
  fi
}

ensure_custom_role() {
  local subscription=$1 name=$2 description=$3 scope=$4 actions_json=$5 file
  if [[ $(az role definition list --subscription "$subscription" --name "$name" --query 'length(@)' --output tsv) == 0 ]]; then
    file=$(mktemp)
    jq -n --arg name "$name" --arg description "$description" --arg scope "$scope" \
      --argjson actions "$actions_json" \
      '{Name:$name,Description:$description,IsCustom:true,Actions:$actions,NotActions:[],DataActions:[],NotDataActions:[],AssignableScopes:[$scope]}' > "$file"
    az role definition create --subscription "$subscription" --role-definition "$file" --output none
    rm -f "$file"
  fi
  az role definition list --subscription "$subscription" --name "$name" --query '[0].name' --output tsv
}

ensure_group "$AZURE_BACKUP_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP"
ensure_group "$AZURE_BACKUP_SUBSCRIPTION_ID" "$AZURE_RESOURCE_GROUP"
ensure_group "$AZURE_SOURCE_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP"
ensure_identity "$AZURE_BACKUP_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP" "$BACKUP_IDENTITY_NAME"
ensure_identity "$AZURE_BACKUP_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP" "$RELEASE_IDENTITY_NAME"
ensure_identity "$AZURE_SOURCE_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP" "$SOURCE_IDENTITY_NAME"
ensure_federation "$AZURE_BACKUP_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP" "$BACKUP_IDENTITY_NAME" backup-infrastructure "$BACKUP_ENVIRONMENT"
ensure_federation "$AZURE_BACKUP_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP" "$RELEASE_IDENTITY_NAME" release "$RELEASE_ENVIRONMENT"
ensure_federation "$AZURE_SOURCE_SUBSCRIPTION_ID" "$OIDC_RESOURCE_GROUP" "$SOURCE_IDENTITY_NAME" source-integration "$SOURCE_ENVIRONMENT"

backup_principal=$(az identity show --subscription "$AZURE_BACKUP_SUBSCRIPTION_ID" --resource-group "$OIDC_RESOURCE_GROUP" \
  --name "$BACKUP_IDENTITY_NAME" --query principalId --output tsv)
backup_client=$(az identity show --subscription "$AZURE_BACKUP_SUBSCRIPTION_ID" --resource-group "$OIDC_RESOURCE_GROUP" \
  --name "$BACKUP_IDENTITY_NAME" --query clientId --output tsv)
release_principal=$(az identity show --subscription "$AZURE_BACKUP_SUBSCRIPTION_ID" --resource-group "$OIDC_RESOURCE_GROUP" \
  --name "$RELEASE_IDENTITY_NAME" --query principalId --output tsv)
release_client=$(az identity show --subscription "$AZURE_BACKUP_SUBSCRIPTION_ID" --resource-group "$OIDC_RESOURCE_GROUP" \
  --name "$RELEASE_IDENTITY_NAME" --query clientId --output tsv)
source_principal=$(az identity show --subscription "$AZURE_SOURCE_SUBSCRIPTION_ID" --resource-group "$OIDC_RESOURCE_GROUP" \
  --name "$SOURCE_IDENTITY_NAME" --query principalId --output tsv)
source_client=$(az identity show --subscription "$AZURE_SOURCE_SUBSCRIPTION_ID" --resource-group "$OIDC_RESOURCE_GROUP" \
  --name "$SOURCE_IDENTITY_NAME" --query clientId --output tsv)

backup_subscription_scope="/subscriptions/$AZURE_BACKUP_SUBSCRIPTION_ID"
backup_group_scope="$backup_subscription_scope/resourceGroups/$AZURE_RESOURCE_GROUP"
source_group_scope="/subscriptions/$AZURE_SOURCE_SUBSCRIPTION_ID/resourceGroups/$source_resource_group"
ensure_role_assignment "$AZURE_BACKUP_SUBSCRIPTION_ID" "$backup_subscription_scope" "$backup_principal" b24988ac-6180-42a0-ab88-20f7382dd24c
ensure_role_assignment "$AZURE_BACKUP_SUBSCRIPTION_ID" "$backup_subscription_scope" "$backup_principal" f58310d9-a9f6-439a-9e8d-f62e7b41a168
ensure_role_assignment "$AZURE_BACKUP_SUBSCRIPTION_ID" "$backup_group_scope" "$release_principal" 8311e382-0749-4cb8-b61a-304f252e45ec

release_role=$(ensure_custom_role "$AZURE_BACKUP_SUBSCRIPTION_ID" 'Cosmos Table Backup Job Image Deployer' \
  'Updates the image on existing Container Apps jobs.' "$backup_subscription_scope" \
  '["Microsoft.App/jobs/read","Microsoft.App/jobs/write"]')
ensure_role_assignment "$AZURE_BACKUP_SUBSCRIPTION_ID" "$backup_group_scope" "$release_principal" "$release_role"

source_role=$(ensure_custom_role "$AZURE_SOURCE_SUBSCRIPTION_ID" 'Cosmos Table Backup Source Integrator' \
  'Approves the expected Cosmos private endpoint and grants the backup Table data reader role.' "$source_group_scope" \
  '["Microsoft.Resources/deployments/*","Microsoft.Resources/subscriptions/resourceGroups/read","Microsoft.DocumentDB/databaseAccounts/read","Microsoft.DocumentDB/databaseAccounts/privateEndpointConnections/read","Microsoft.DocumentDB/databaseAccounts/privateEndpointConnections/write","Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions/read","Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/read","Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/write"]')
ensure_role_assignment "$AZURE_SOURCE_SUBSCRIPTION_ID" "$source_group_scope" "$source_principal" "$source_role"

set_environment_variable() {
  local environment=$1 name=$2 value=$3
  gh variable set "$name" --repo "$GITHUB_REPOSITORY" --env "$environment" --body "$value"
}

configure_environment() {
  local environment=$1 reviewer_id=$2
  jq -n --argjson reviewer "$reviewer_id" \
    '{wait_timer:0,prevent_self_review:false,reviewers:[{type:"User",id:$reviewer}],deployment_branch_policy:{protected_branches:false,custom_branch_policies:true}}' | \
    gh api --method PUT "repos/$GITHUB_REPOSITORY/environments/$environment" --input - >/dev/null
}

ensure_deployment_policy() {
  local environment=$1 name=$2 type=$3 count
  count=$(gh api "repos/$GITHUB_REPOSITORY/environments/$environment/deployment-branch-policies" | \
    jq --arg name "$name" --arg type "$type" '[.branch_policies[] | select(.name == $name and .type == $type)] | length')
  if [[ "$count" == 0 ]]; then
    jq -n --arg name "$name" --arg type "$type" '{name:$name,type:$type}' | \
      gh api --method POST "repos/$GITHUB_REPOSITORY/environments/$environment/deployment-branch-policies" --input - >/dev/null
  fi
}

if [[ "$CONFIGURE_GITHUB" == "true" ]]; then
  reviewer_id=$(gh api user --jq .id)
  for environment in "$BACKUP_ENVIRONMENT" "$SOURCE_ENVIRONMENT" "$RELEASE_ENVIRONMENT"; do
    configure_environment "$environment" "$reviewer_id"
    ensure_deployment_policy "$environment" "$DEPLOYMENT_BRANCH" branch
  done
  ensure_deployment_policy "$RELEASE_ENVIRONMENT" 'v*' tag
  ensure_deployment_policy "$BACKUP_ENVIRONMENT" 'v*' tag

  set_environment_variable "$BACKUP_ENVIRONMENT" AZURE_TENANT_ID "$backup_tenant"
  set_environment_variable "$BACKUP_ENVIRONMENT" AZURE_BACKUP_SUBSCRIPTION_ID "$AZURE_BACKUP_SUBSCRIPTION_ID"
  set_environment_variable "$BACKUP_ENVIRONMENT" AZURE_BACKUP_DEPLOY_CLIENT_ID "$backup_client"
  set_environment_variable "$BACKUP_ENVIRONMENT" SOURCE_COSMOS_ACCOUNT_RESOURCE_ID "$SOURCE_COSMOS_ACCOUNT_RESOURCE_ID"
  set_environment_variable "$BACKUP_ENVIRONMENT" AZURE_LOCATION "$AZURE_LOCATION"
  set_environment_variable "$BACKUP_ENVIRONMENT" AZURE_RESOURCE_GROUP "$AZURE_RESOURCE_GROUP"
  set_environment_variable "$BACKUP_ENVIRONMENT" BACKUP_JOB_NAME "$BACKUP_JOB_NAME"

  set_environment_variable "$SOURCE_ENVIRONMENT" AZURE_TENANT_ID "$source_tenant"
  set_environment_variable "$SOURCE_ENVIRONMENT" AZURE_SOURCE_SUBSCRIPTION_ID "$AZURE_SOURCE_SUBSCRIPTION_ID"
  set_environment_variable "$SOURCE_ENVIRONMENT" AZURE_SOURCE_DEPLOY_CLIENT_ID "$source_client"
  set_environment_variable "$SOURCE_ENVIRONMENT" SOURCE_COSMOS_ACCOUNT_RESOURCE_ID "$SOURCE_COSMOS_ACCOUNT_RESOURCE_ID"

  set_environment_variable "$RELEASE_ENVIRONMENT" AZURE_TENANT_ID "$backup_tenant"
  set_environment_variable "$RELEASE_ENVIRONMENT" AZURE_BACKUP_SUBSCRIPTION_ID "$AZURE_BACKUP_SUBSCRIPTION_ID"
  set_environment_variable "$RELEASE_ENVIRONMENT" AZURE_RELEASE_CLIENT_ID "$release_client"
  set_environment_variable "$RELEASE_ENVIRONMENT" AZURE_RESOURCE_GROUP "$AZURE_RESOURCE_GROUP"
  set_environment_variable "$RELEASE_ENVIRONMENT" BACKUP_JOB_NAME "$BACKUP_JOB_NAME"
  set_environment_variable "$RELEASE_ENVIRONMENT" RESTORE_JOB_NAME "$RESTORE_JOB_NAME"

  registry_name=$(az acr list --subscription "$AZURE_BACKUP_SUBSCRIPTION_ID" --resource-group "$AZURE_RESOURCE_GROUP" \
    --query '[0].name' --output tsv)
  registry_server=$(az acr list --subscription "$AZURE_BACKUP_SUBSCRIPTION_ID" --resource-group "$AZURE_RESOURCE_GROUP" \
    --query '[0].loginServer' --output tsv)
  if [[ -n "$registry_name" && -n "$registry_server" ]]; then
    set_environment_variable "$BACKUP_ENVIRONMENT" ACR_LOGIN_SERVER "$registry_server"
    set_environment_variable "$RELEASE_ENVIRONMENT" ACR_NAME "$registry_name"
    set_environment_variable "$RELEASE_ENVIRONMENT" ACR_LOGIN_SERVER "$registry_server"
  else
    echo 'ACR does not exist yet. Re-run this script after the first infrastructure deployment.' >&2
  fi
fi

jq -n \
  --arg repository "$GITHUB_REPOSITORY" \
  --arg backupClientId "$backup_client" \
  --arg releaseClientId "$release_client" \
  --arg sourceClientId "$source_client" \
  '{repository:$repository,backupDeployClientId:$backupClientId,releaseClientId:$releaseClientId,sourceDeployClientId:$sourceClientId}'
