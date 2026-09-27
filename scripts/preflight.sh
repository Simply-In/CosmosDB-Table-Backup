#!/usr/bin/env bash
set -euo pipefail

TARGET_SUBSCRIPTION="${TARGET_SUBSCRIPTION_ID:?TARGET_SUBSCRIPTION_ID is required}"
SOURCE_RESOURCE_ID="${SOURCE_COSMOS_RESOURCE_ID:?SOURCE_COSMOS_RESOURCE_ID is required}"
SOURCE_SUBSCRIPTION="$(cut -d/ -f3 <<<"$SOURCE_RESOURCE_ID")"

command -v az >/dev/null || { echo "Azure CLI is required" >&2; exit 1; }
az account show --output none >/dev/null || { echo "Run 'az login' first" >&2; exit 1; }

tenant() {
  local subscription="$1"
  az account show --subscription "$subscription" --query tenantId --output tsv
}

TARGET_TENANT="$(tenant "$TARGET_SUBSCRIPTION")"
SOURCE_TENANT="$(tenant "$SOURCE_SUBSCRIPTION")"
[[ "$TARGET_TENANT" == "$SOURCE_TENANT" ]] || { echo "Subscriptions are in different tenants" >&2; exit 1; }

ACCOUNT_JSON="$(az cosmosdb show --ids "$SOURCE_RESOURCE_ID" --output json)"
python3 - "$ACCOUNT_JSON" <<'PY'
import json, sys
account = json.loads(sys.argv[1])
capabilities = {c["name"] for c in account.get("capabilities", [])}
failures = []
if "EnableTable" not in capabilities:
    failures.append("source does not expose the Table API")
if account.get("publicNetworkAccess", "Enabled").lower() != "disabled":
    failures.append("source public network access is not disabled")
if not account.get("disableLocalAuth", False):
    failures.append("source local authentication is not disabled")
if failures:
    raise SystemExit("Preflight failed: " + "; ".join(failures))
print("Source account invariants: OK")
PY

SOURCE_RESOURCE_GROUP="$(cut -d/ -f5 <<<"$SOURCE_RESOURCE_ID")"
SOURCE_ACCOUNT="$(cut -d/ -f9 <<<"$SOURCE_RESOURCE_ID")"
az cosmosdb sql role definition list \
  --subscription "$SOURCE_SUBSCRIPTION" \
  --resource-group "$SOURCE_RESOURCE_GROUP" \
  --account-name "$SOURCE_ACCOUNT" \
  --query "[?roleName=='Cosmos DB Built-in Data Reader'].roleName | [0]" \
  --output tsv | grep -qx "Cosmos DB Built-in Data Reader" || {
    echo "Cosmos DB Built-in Data Reader role not found at source account" >&2
    exit 1
  }

echo "Target/source tenant: $TARGET_TENANT"
echo "Target subscription: $TARGET_SUBSCRIPTION"
echo "Source account: $SOURCE_RESOURCE_ID"
echo "Preflight passed (read-only; no resources changed)."
