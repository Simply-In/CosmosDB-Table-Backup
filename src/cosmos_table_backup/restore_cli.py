"""Fail-closed restore command-line entry point."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sys
from collections.abc import Sequence

from azure.data.tables import TableServiceClient
from azure.identity import ManagedIdentityCredential
from azure.keyvault.keys.crypto import CryptographyClient
from azure.storage.blob import BlobServiceClient

from cosmos_table_backup.config import ConfigurationError
from cosmos_table_backup.restore import RestoreRunner
from cosmos_table_backup.restore_config import RestoreConfig
from cosmos_table_backup.restore_storage import AzureRestoreSource
from cosmos_table_backup.telemetry import SafeLogger, configure_logging, configure_monitor_export

_RESTORE_FAILED_EVENT = "restore.failed"


def _silence_sdk_logging() -> None:
    # SDK HTTP logs are not an allowlisted telemetry channel, even at INFO.
    for name in (
        "azure",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.data.tables",
        "azure.storage.blob",
        "azure.identity",
        "azure.keyvault",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    for name, logger in logging.Logger.manager.loggerDict.items():
        if name.startswith("azure.") and isinstance(logger, logging.Logger):
            logger.setLevel(logging.CRITICAL + 1)
            logger.disabled = True


def _private_plan_logger() -> SafeLogger:
    logger = logging.getLogger("cosmos_table_backup.private_plan")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return SafeLogger(logger)


def _emit_failure(logger: SafeLogger, exc: Exception) -> None:
    logger.emit(_RESTORE_FAILED_EVENT, status="failed", error_type=type(exc).__name__)


def plan_frames(plan: str) -> tuple[str, ...]:
    """Frame private control metadata for bounded container console lines, never telemetry."""
    payload = plan.encode("utf-8")
    if not payload or len(payload) > 16 * 1024:
        raise ValueError("private plan must be between 1 byte and 16 KiB")
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode("ascii")
    chunks = [encoded[offset : offset + 384] for offset in range(0, len(encoded), 384)]
    total = len(chunks)
    return (
        *(
            f"restore.plan.part:{index}/{total}:{digest}:{chunk}"
            for index, chunk in enumerate(chunks)
        ),
        f"restore.plan.complete:{total}:{digest}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    planning = list(argv) == ["--plan"]
    data_only = list(argv) == ["--data-only"]
    if argv and not planning and not data_only:
        print("usage: cosmos-table-restore [--plan | --data-only]", file=sys.stderr)
        return 2
    _silence_sdk_logging()
    logger = _private_plan_logger() if planning else configure_logging("INFO")
    credential = None
    client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not planning and client_id and connection_string:
        try:
            credential = ManagedIdentityCredential(client_id=client_id)
            configure_monitor_export(credential, connection_string)
        except Exception as exc:
            _emit_failure(logger, exc)
            return 1
    try:
        config = RestoreConfig.from_env(
            {**os.environ, "RESTORE_DATA_ONLY": "true"} if data_only else None
        )
    except ConfigurationError as exc:
        _emit_failure(logger, exc)
        if not planning:
            print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if not planning:
        logger = configure_logging(config.log_level)
    runner_started = False
    try:
        if credential is None:
            credential = ManagedIdentityCredential(client_id=config.managed_identity_client_id)
            if not planning:
                configure_monitor_export(credential, config.application_insights_connection_string)
        _silence_sdk_logging()
        blob_service = BlobServiceClient(config.backup_storage_account_url, credential=credential)
        target_service = (
            None
            if planning
            else TableServiceClient(
                config.target_table_endpoint,
                credential=credential,
                audience="https://cosmos.azure.com",
            )
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
        if planning:
            for frame in plan_frames(runner.plan()):
                print(frame)
            from cosmos_table_backup.supervisor import collection_window

            collection_window()
            return 0
        runner_started = True
        report = runner.run()
        print(report.to_json())
        from cosmos_table_backup.supervisor import collection_window

        collection_window()
        return 0
    except Exception as exc:
        if not runner_started:
            _emit_failure(logger, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
