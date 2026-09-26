#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE:?Set IMAGE to the immutable job image reference}"
: "${ACR_LOGIN_SERVER:?Set ACR_LOGIN_SERVER}"
: "${AZURE_RESOURCE_GROUP:?Set AZURE_RESOURCE_GROUP}"
: "${BACKUP_JOB_NAME:?Set BACKUP_JOB_NAME}"
: "${RESTORE_JOB_NAME:?Set RESTORE_JOB_NAME}"

prefix="$ACR_LOGIN_SERVER/cosmos-table-backup@sha256:"
digest=${IMAGE#"$prefix"}
if [[ "$IMAGE" != "$prefix"* || ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
  echo 'IMAGE must use the expected ACR repository and an immutable sha256 digest.' >&2
  exit 1
fi

az extension add --name containerapp --upgrade --yes
for job in "$BACKUP_JOB_NAME" "$RESTORE_JOB_NAME"; do
  az containerapp job update --resource-group "$AZURE_RESOURCE_GROUP" --name "$job" \
    --image "$IMAGE" --output none
  actual=$(az containerapp job show --resource-group "$AZURE_RESOURCE_GROUP" --name "$job" \
    --query 'properties.template.containers[0].image' --output tsv)
  if [[ "$actual" != "$IMAGE" ]]; then
    echo "Job $job reports unexpected image $actual" >&2
    exit 1
  fi
done
