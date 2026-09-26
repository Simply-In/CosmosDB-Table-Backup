"""Typed environment configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when runtime configuration is invalid."""


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _https_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must be an HTTPS service URL")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class BackupConfig:
    table_endpoint: str
    storage_account_url: str
    container_name: str
    key_id: str
    managed_identity_client_id: str | None = None
    excluded_tables: frozenset[str] = frozenset({"cards"})
    application_insights_connection_string: str = ""
    application_insights_authentication_string: str = ""
    page_size: int = 500
    block_size: int = 4 * 1024 * 1024
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BackupConfig:
        values = dict(os.environ if env is None else env)
        try:
            page_size = int(values.get("BACKUP_PAGE_SIZE", "500"))
            block_size = int(values.get("BACKUP_BLOCK_SIZE", str(4 * 1024 * 1024)))
        except ValueError as exc:
            raise ConfigurationError("page and block sizes must be integers") from exc
        if not 1 <= page_size <= 1000:
            raise ConfigurationError("BACKUP_PAGE_SIZE must be between 1 and 1000")
        if not 64 * 1024 <= block_size <= 100 * 1024 * 1024:
            raise ConfigurationError("BACKUP_BLOCK_SIZE must be between 64 KiB and 100 MiB")
        table_endpoint = _https_url(
            _required(values, "COSMOS_TABLE_ENDPOINT"), "COSMOS_TABLE_ENDPOINT"
        )
        storage_url = _https_url(
            _required(values, "BACKUP_STORAGE_ACCOUNT_URL"), "BACKUP_STORAGE_ACCOUNT_URL"
        )
        container = _required(values, "BACKUP_CONTAINER_NAME")
        if not (
            3 <= len(container) <= 63
            and container.replace("-", "").isalnum()
            and container == container.lower()
        ):
            raise ConfigurationError(
                "BACKUP_CONTAINER_NAME is not a valid lowercase container name"
            )
        key_id = _https_url(_required(values, "BACKUP_KEY_ID"), "BACKUP_KEY_ID")
        parts = urlparse(key_id).path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "keys" or not all(parts[1:]):
            raise ConfigurationError("BACKUP_KEY_ID must identify an exact key version")
        client_id = _required(values, "AZURE_CLIENT_ID")
        insights_connection = _required(values, "APPLICATIONINSIGHTS_CONNECTION_STRING")
        insights_authentication = _required(values, "APPLICATIONINSIGHTS_AUTHENTICATION_STRING")
        if insights_authentication != f"Authorization=AAD;ClientId={client_id}":
            raise ConfigurationError(
                "APPLICATIONINSIGHTS_AUTHENTICATION_STRING must use the backup identity"
            )
        try:
            raw_exclusions = json.loads(_required(values, "EXCLUDED_TABLES_JSON"))
        except json.JSONDecodeError as exc:
            raise ConfigurationError("EXCLUDED_TABLES_JSON must be valid JSON") from exc
        if (
            not isinstance(raw_exclusions, list)
            or any(not isinstance(name, str) or not name for name in raw_exclusions)
            or len(set(raw_exclusions)) != len(raw_exclusions)
        ):
            raise ConfigurationError("EXCLUDED_TABLES_JSON must be an array of unique names")
        exclusions = frozenset(raw_exclusions) | {"cards"}
        return cls(
            table_endpoint=table_endpoint,
            storage_account_url=storage_url,
            container_name=container,
            key_id=key_id,
            managed_identity_client_id=client_id,
            excluded_tables=frozenset(exclusions),
            application_insights_connection_string=insights_connection,
            application_insights_authentication_string=insights_authentication,
            page_size=page_size,
            block_size=block_size,
            log_level=values.get("LOG_LEVEL", "INFO").upper(),
        )
