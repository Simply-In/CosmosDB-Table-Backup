import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from cosmos_table_backup.backup import BackupError
from cosmos_table_backup.cli import _run_backup, main
from cosmos_table_backup.config import BackupConfig, ConfigurationError

ENV = {
    "COSMOS_TABLE_ENDPOINT": "https://source.table.cosmos.azure.com",
    "BACKUP_STORAGE_ACCOUNT_URL": "https://backup.blob.core.windows.net",
    "BACKUP_CONTAINER_NAME": "backups",
    "BACKUP_KEY_ID": "https://vault.vault.azure.net/keys/backup/version1",
    "AZURE_CLIENT_ID": "backup-id",
    "EXCLUDED_TABLES_JSON": '["cards"]',
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=test",
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": "Authorization=AAD;ClientId=backup-id",
}


def test_cli_rejects_arguments() -> None:
    assert main(["unexpected"]) == 2


def test_cli_configuration_failure_is_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cosmos_table_backup.cli.BackupConfig.from_env",
        Mock(side_effect=ConfigurationError("bad")),
    )
    assert main([]) == 2


def test_cli_uses_official_client_signatures_and_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    credential = Mock()
    default_credential = Mock(return_value=credential)
    async_credential = AsyncMock()
    async_credential.__aenter__.return_value = async_credential
    async_credential_constructor = Mock(return_value=async_credential)
    table_service = AsyncMock()
    table_service.__aenter__.return_value = table_service
    table_constructor = Mock(return_value=table_service)
    blob_service = AsyncMock()
    blob_service.__aenter__.return_value = blob_service
    blob_service.get_container_client = Mock()
    blob_constructor = Mock(return_value=blob_service)
    crypto_client = AsyncMock()
    crypto_client.__aenter__.return_value = crypto_client
    crypto_constructor = Mock(return_value=crypto_client)
    runner = Mock()
    runner.run = AsyncMock()
    runner_constructor = Mock(return_value=runner)
    monitor = Mock()
    monkeypatch.setattr("cosmos_table_backup.cli.DefaultAzureCredential", default_credential)
    monkeypatch.setattr(
        "cosmos_table_backup.cli.AsyncDefaultAzureCredential", async_credential_constructor
    )
    monkeypatch.setattr("cosmos_table_backup.cli.configure_monitor_export", monitor)
    monkeypatch.setattr("cosmos_table_backup.cli.TableServiceClient", table_constructor)
    monkeypatch.setattr("cosmos_table_backup.cli.BlobServiceClient", blob_constructor)
    monkeypatch.setattr("cosmos_table_backup.cli.CryptographyClient", crypto_constructor)
    monkeypatch.setattr("cosmos_table_backup.cli.BackupRunner", runner_constructor)

    assert main([]) == 0
    default_credential.assert_called_once_with(managed_identity_client_id="backup-id")
    monitor.assert_called_once_with(credential, "InstrumentationKey=test")
    table_constructor.assert_called_once_with(
        ENV["COSMOS_TABLE_ENDPOINT"],
        credential=async_credential,
        audience="https://cosmos.azure.com",
    )
    blob_constructor.assert_called_once_with(
        ENV["BACKUP_STORAGE_ACCOUNT_URL"], credential=async_credential
    )
    crypto_constructor.assert_called_once_with(ENV["BACKUP_KEY_ID"], async_credential)
    runner.run.assert_awaited_once_with()
    credential.close.assert_called_once_with()
    for client in (async_credential, table_service, blob_service, crypto_client):
        client.__aexit__.assert_awaited_once()


def test_cli_runtime_failure_returns_nonzero_and_emits_expected_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(
        "cosmos_table_backup.cli.BackupConfig.from_env",
        Mock(return_value=Mock(log_level="INFO", managed_identity_client_id=None)),
    )
    logger = Mock()
    monkeypatch.setattr("cosmos_table_backup.cli.configure_logging", Mock(return_value=logger))
    monkeypatch.setattr(
        "cosmos_table_backup.cli.DefaultAzureCredential", Mock(side_effect=RuntimeError("auth"))
    )
    assert main([]) == 1
    logger.emit.assert_called_once_with("backup.failed", status="failed", error_type="RuntimeError")


@pytest.mark.parametrize(
    "failure", [RuntimeError("startup"), BackupError("pipeline"), asyncio.CancelledError()]
)
def test_async_client_cleanup_preserves_failures(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    clients = [AsyncMock() for _ in range(4)]
    names = [
        "AsyncDefaultAzureCredential",
        "TableServiceClient",
        "BlobServiceClient",
        "CryptographyClient",
    ]
    for name, client in zip(names, clients, strict=True):
        client.__aenter__.return_value = client
        monkeypatch.setattr(f"cosmos_table_backup.cli.{name}", Mock(return_value=client))
    clients[2].get_container_client = Mock()
    runner = Mock(run=AsyncMock(side_effect=failure))
    monkeypatch.setattr("cosmos_table_backup.cli.BackupRunner", Mock(return_value=runner))
    config = BackupConfig.from_env(ENV)
    with pytest.raises(type(failure)):
        asyncio.run(_run_backup(config, Mock()))
    for client in clients:
        client.__aexit__.assert_awaited_once()


def test_monitor_credential_closes_on_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    credential = Mock()
    monkeypatch.setattr(
        "cosmos_table_backup.cli.DefaultAzureCredential", Mock(return_value=credential)
    )
    monkeypatch.setattr("cosmos_table_backup.cli.configure_monitor_export", Mock())
    monkeypatch.setattr(
        "cosmos_table_backup.cli.BackupConfig.from_env",
        Mock(side_effect=ConfigurationError("invalid")),
    )
    assert main([]) == 2
    credential.close.assert_called_once_with()


def test_backup_suppresses_sdk_http_logs_and_reports_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    raw = logging.getLogger("azure.storage.blob.test_http")
    raw.setLevel(logging.DEBUG)
    credential = Mock()
    credential.close.side_effect = RuntimeError("unsafe SDK close details")
    monkeypatch.setattr(
        "cosmos_table_backup.cli.DefaultAzureCredential", Mock(return_value=credential)
    )
    monkeypatch.setattr("cosmos_table_backup.cli.configure_monitor_export", Mock())
    monkeypatch.setattr("cosmos_table_backup.cli._run_backup", AsyncMock())
    monkeypatch.setattr("cosmos_table_backup.supervisor.collection_window", Mock())
    logger = Mock()
    monkeypatch.setattr("cosmos_table_backup.cli.configure_logging", Mock(return_value=logger))
    assert main([]) == 1
    assert raw.disabled
    logger.emit.assert_called_once_with("backup.failed", status="failed", error_type="RuntimeError")
