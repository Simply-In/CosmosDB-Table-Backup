from unittest.mock import Mock

import pytest

from cosmos_table_backup.cli import main
from cosmos_table_backup.config import ConfigurationError

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
    table_service = Mock()
    table_constructor = Mock(return_value=table_service)
    blob_service = Mock()
    blob_constructor = Mock(return_value=blob_service)
    crypto_client = Mock()
    crypto_constructor = Mock(return_value=crypto_client)
    runner = Mock()
    runner_constructor = Mock(return_value=runner)
    monitor = Mock()
    monkeypatch.setattr("cosmos_table_backup.cli.DefaultAzureCredential", default_credential)
    monkeypatch.setattr("cosmos_table_backup.cli.configure_monitor_export", monitor)
    monkeypatch.setattr("cosmos_table_backup.cli.TableServiceClient", table_constructor)
    monkeypatch.setattr("cosmos_table_backup.cli.BlobServiceClient", blob_constructor)
    monkeypatch.setattr("cosmos_table_backup.cli.CryptographyClient", crypto_constructor)
    monkeypatch.setattr("cosmos_table_backup.cli.BackupRunner", runner_constructor)

    assert main([]) == 0
    default_credential.assert_called_once_with(managed_identity_client_id="backup-id")
    monitor.assert_called_once_with(credential, "InstrumentationKey=test")
    table_constructor.assert_called_once_with(
        ENV["COSMOS_TABLE_ENDPOINT"], credential=credential, audience="https://cosmos.azure.com"
    )
    blob_constructor.assert_called_once_with(
        ENV["BACKUP_STORAGE_ACCOUNT_URL"], credential=credential
    )
    crypto_constructor.assert_called_once_with(ENV["BACKUP_KEY_ID"], credential)
    runner.run.assert_called_once_with()


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
