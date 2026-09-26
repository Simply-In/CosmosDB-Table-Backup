"""Fail-closed restore command-line entry point."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.keyvault.keys.crypto import CryptographyClient
from azure.storage.blob import BlobServiceClient

from cosmos_table_backup.config import ConfigurationError
from cosmos_table_backup.restore import RestoreRunner
from cosmos_table_backup.restore_config import RestoreConfig
from cosmos_table_backup.restore_storage import AzureRestoreSource
from cosmos_table_backup.telemetry import configure_logging, configure_monitor_export


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("usage: cosmos-table-restore", file=sys.stderr)
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
            logger.emit("restore.failed", status="failed", error_type=type(exc).__name__)
            return 1
    try:
        config = RestoreConfig.from_env()
    except ConfigurationError as exc:
        logger.emit("restore.failed", status="failed", error_type=type(exc).__name__)
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
        blob_service = BlobServiceClient(config.backup_storage_account_url, credential=credential)
        target_service = TableServiceClient(
            config.target_table_endpoint,
            credential=credential,
            audience="https://cosmos.azure.com",
        )
        source = AzureRestoreSource(
            blob_service.get_container_client(config.backup_container_name),
            config.download_chunk_size,
        )
        runner = RestoreRunner(
            config,
            source,
            target_service,
            lambda key_id: CryptographyClient(key_id, credential),
            logger,
        )
        runner_started = True
        report = runner.run()
        print(report.to_json())
        return 0
    except Exception as exc:
        if not runner_started:
            logger.emit("restore.failed", status="failed", error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
