#!/usr/bin/env bash
set -euo pipefail

: "${IMAGE:?Set IMAGE to the local image to validate}"

architecture=$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')
if [[ "$architecture" != 'linux/amd64' ]]; then
  echo 'The job image must target linux/amd64.' >&2
  exit 1
fi

docker run --rm --network none --platform linux/amd64 "$IMAGE" python -c \
  'import os; assert os.getuid() != 0, "The runtime user must not be root"'

output=$(mktemp)
trap 'rm -f "$output"' EXIT
check_startup() {
  local event=$1
  shift
  local result=0
  docker run --rm --network none --platform linux/amd64 "$IMAGE" "$@" > "$output" 2>&1 || result=$?
  if [[ "$result" -ne 2 ]] || ! grep -q 'configuration error:' "$output" || \
      ! grep -Fq "\"event\":\"$event\"" "$output"; then
    echo "Image startup failed for $event; expected a fail-closed configuration error (exit 2)." >&2
    exit 1
  fi
}

# No credentials or network: validate startup, not Azure backup or recoverability.
check_startup backup.failed
check_startup restore.failed python -m cosmos_table_backup.cli restore-test
check_startup restore.failed python -m cosmos_table_backup.restore_cli
printf 'Image startup checks passed (non-root Linux AMD64, backup and restore).\n'
