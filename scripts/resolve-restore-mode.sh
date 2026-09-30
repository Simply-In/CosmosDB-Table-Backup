#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_ENV:?GITHUB_ENV is required}"
for value in "${INPUT_RESTORE_ACCESS-false}" "${INPUT_ENABLE_RESTORE-false}"; do
  case "$value" in
    true|false) ;;
    *) echo 'Restore inputs must be true or false' >&2; exit 1 ;;
  esac
done
access=false
if [[ "${INPUT_RESTORE_ACCESS:-false}" == true || "${INPUT_ENABLE_RESTORE:-false}" == true ]]; then
  access=true
fi
printf 'RESTORE_ACCESS_ENABLED=%s\nRESTORE_SCHEDULE_ENABLED=%s\n' \
  "$access" "${INPUT_ENABLE_RESTORE:-false}" >> "$GITHUB_ENV"
