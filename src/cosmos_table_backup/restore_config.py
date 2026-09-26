"""Typed, guarded restore configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

from cosmos_table_backup.config import ConfigurationError, _https_url, _required


def _account_name(url: str) -> str:
    hostname = urlparse(url).hostname
    if hostname is None:
        raise ConfigurationError("service URL has no host")
    return hostname.split(".", 1)[0].lower()


def _resource_id(value: str, name: str) -> str:
    normalized = value.strip().rstrip("/").lower()
    parts = normalized.split("/")
    if not normalized.startswith("/subscriptions/") or len(parts) < 9:
        raise ConfigurationError(f"{name} must be an Azure resource ID")
    return normalized


@dataclass(frozen=True, slots=True)
class RestoreConfig:
    source_account_resource_id: str
    backup_storage_account_url: str
    backup_container_name: str
    target_account_resource_id: str
    target_table_endpoint: str
    expected_key_id: str
    backup_id: str | None
    managed_identity_client_id: str
    application_insights_connection_string: str
    application_insights_authentication_string: str
    batch_size: int = 100
    max_record_bytes: int = 4 * 1024 * 1024
    max_manifest_bytes: int = 16 * 1024 * 1024
    download_chunk_size: int = 4 * 1024 * 1024
    max_batch_payload_bytes: int = 1_250_000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RestoreConfig:
        values = dict(os.environ if env is None else env)
        storage = _https_url(
            _required(values, "BACKUP_STORAGE_ACCOUNT_URL"), "BACKUP_STORAGE_ACCOUNT_URL"
        )
        target = _https_url(
            _required(values, "RESTORE_TARGET_TABLE_ENDPOINT"),
            "RESTORE_TARGET_TABLE_ENDPOINT",
        )
        source_resource_id = _resource_id(
            _required(values, "RESTORE_SOURCE_ACCOUNT_RESOURCE_ID"),
            "RESTORE_SOURCE_ACCOUNT_RESOURCE_ID",
        )
        target_resource_id = _resource_id(
            _required(values, "RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID"),
            "RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID",
        )
        if values.get("RESTORE_REQUIRE_ISOLATED_TARGET", "").lower() != "true":
            raise ConfigurationError("RESTORE_REQUIRE_ISOLATED_TARGET must be true")
        if source_resource_id == target_resource_id:
            raise ConfigurationError("restore target resource must differ from source resource")
        target_account = target_resource_id.rsplit("/", 1)[-1]
        if _account_name(target) != target_account:
            raise ConfigurationError("restore target endpoint does not match target resource ID")
        if _account_name(target) == _account_name(storage):
            raise ConfigurationError("restore target must differ from backup storage account")
        backup_id = values.get("RESTORE_BACKUP_ID", "").strip() or None
        if backup_id is not None:
            try:
                UUID(backup_id)
            except ValueError as exc:
                raise ConfigurationError("RESTORE_BACKUP_ID must be a UUID when set") from exc
        client_id = _required(values, "AZURE_CLIENT_ID")
        insights_connection = _required(values, "APPLICATIONINSIGHTS_CONNECTION_STRING")
        insights_authentication = _required(values, "APPLICATIONINSIGHTS_AUTHENTICATION_STRING")
        expected_insights_authentication = f"Authorization=AAD;ClientId={client_id}"
        if insights_authentication != expected_insights_authentication:
            raise ConfigurationError(
                "APPLICATIONINSIGHTS_AUTHENTICATION_STRING must use the restore identity"
            )
        expected_key_id = _https_url(_required(values, "KEY_VAULT_KEY_ID"), "KEY_VAULT_KEY_ID")
        key_parts = urlparse(expected_key_id).path.strip("/").split("/")
        if len(key_parts) != 2 or key_parts[0] != "keys" or not key_parts[1]:
            raise ConfigurationError("KEY_VAULT_KEY_ID must identify an unversioned key")
        try:
            batch_size = int(values.get("RESTORE_BATCH_SIZE", "100"))
            max_record_bytes = int(values.get("RESTORE_MAX_RECORD_BYTES", str(4 * 1024 * 1024)))
            max_manifest_bytes = int(
                values.get("RESTORE_MAX_MANIFEST_BYTES", str(16 * 1024 * 1024))
            )
            chunk_size = int(values.get("RESTORE_DOWNLOAD_CHUNK_SIZE", str(4 * 1024 * 1024)))
            max_batch_payload_bytes = int(values.get("RESTORE_MAX_BATCH_PAYLOAD_BYTES", "1250000"))
        except ValueError as exc:
            raise ConfigurationError("restore bounds must be integers") from exc
        if not 1 <= batch_size <= 100:
            raise ConfigurationError("RESTORE_BATCH_SIZE must be between 1 and 100")
        if min(max_record_bytes, max_manifest_bytes, chunk_size) < 64 * 1024:
            raise ConfigurationError("restore byte bounds must be at least 64 KiB")
        if not 64 * 1024 <= max_batch_payload_bytes <= 1_500_000:
            raise ConfigurationError(
                "RESTORE_MAX_BATCH_PAYLOAD_BYTES must be between 64 KiB and 1.5 MB"
            )
        return cls(
            source_account_resource_id=source_resource_id,
            backup_storage_account_url=storage,
            backup_container_name=_required(values, "BACKUP_CONTAINER"),
            target_account_resource_id=target_resource_id,
            target_table_endpoint=target,
            expected_key_id=expected_key_id,
            backup_id=backup_id,
            managed_identity_client_id=client_id,
            application_insights_connection_string=insights_connection,
            application_insights_authentication_string=insights_authentication,
            batch_size=batch_size,
            max_record_bytes=max_record_bytes,
            max_manifest_bytes=max_manifest_bytes,
            download_chunk_size=chunk_size,
            max_batch_payload_bytes=max_batch_payload_bytes,
            log_level=values.get("LOG_LEVEL", "INFO").upper(),
        )
