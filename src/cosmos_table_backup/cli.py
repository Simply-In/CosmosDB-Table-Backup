"""Command-line entry point."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.keyvault.keys.crypto import CryptographyClient
from azure.storage.blob import BlobServiceClient

from cosmos_table_backup.backup import BackupRunner
from cosmos_table_backup.config import BackupConfig, ConfigurationError
from cosmos_table_backup.telemetry import SafeLogger, configure_logging, configure_monitor_export

_BACKUP_FAILED_EVENT = "backup.failed"


def _emit_failure(logger: SafeLogger, exc: Exception) -> None:
    logger.emit(_BACKUP_FAILED_EVENT, status="failed", error_type=type(exc).__name__)


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "restore-test":
        from cosmos_table_backup.restore_cli import main as restore_main

        return restore_main(argv[1:])
    if argv:
        print("usage: cosmos-table-backup [restore-test]", file=sys.stderr)
        return 2
    logger = configure_logging("INFO")
    credential = None
    client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if client_id and connection_string:
        try:
            credential = DefaultAzureCredential(managed_identity_client_id=client_id)
            configure_monitor_export(credential, connection_string)
        except Exception as exc:
            _emit_failure(logger, exc)
            return 1
    try:
        config = BackupConfig.from_env()
    except ConfigurationError as exc:
        _emit_failure(logger, exc)
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logger = configure_logging(config.log_level)
    runner_started = False
    try:
        if credential is None:
            credential = DefaultAzureCredential(
                managed_identity_client_id=config.managed_identity_client_id
            )
            configure_monitor_export(credential, config.application_insights_connection_string)
        table_service = TableServiceClient(
            config.table_endpoint,
            credential=credential,
            audience="https://cosmos.azure.com",
        )
        blob_service = BlobServiceClient(config.storage_account_url, credential=credential)
        crypto_client = CryptographyClient(config.key_id, credential)
        runner = BackupRunner(
            config,
            table_service,
            blob_service.get_container_client(config.container_name),
            crypto_client,
            logger,
        )
        runner_started = True
        runner.run()
        from cosmos_table_backup.supervisor import collection_window

        collection_window()
        return 0
    except Exception as exc:
        if not runner_started:
            _emit_failure(logger, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
