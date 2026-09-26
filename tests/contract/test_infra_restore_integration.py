from unittest.mock import Mock

import pytest

from cosmos_table_backup.cli import main

INFRA_ENV = {
    "AZURE_CLIENT_ID": "restore-client-id",
    "BACKUP_STORAGE_ACCOUNT_URL": "https://backupacct.blob.core.windows.net",
    "BACKUP_CONTAINER": "backups",
    "KEY_VAULT_KEY_ID": "https://vault.vault.azure.net/keys/backup-kek",
    "RESTORE_TARGET_COSMOS_ACCOUNT_RESOURCE_ID": (
        "/subscriptions/s/resourceGroups/r/providers/Microsoft.DocumentDB/databaseAccounts/isolated"
    ),
    "RESTORE_TARGET_TABLE_ENDPOINT": "https://isolated.table.cosmos.azure.com",
    "RESTORE_SOURCE_ACCOUNT_RESOURCE_ID": (
        "/subscriptions/s/resourceGroups/r/providers/Microsoft.DocumentDB/databaseAccounts/source"
    ),
    "RESTORE_REQUIRE_ISOLATED_TARGET": "true",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=test",
    "APPLICATIONINSIGHTS_AUTHENTICATION_STRING": ("Authorization=AAD;ClientId=restore-client-id"),
}


def test_infra_command_and_environment_dispatch_restore(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in INFRA_ENV.items():
        monkeypatch.setenv(name, value)
    credential = Mock()
    monkeypatch.setattr(
        "cosmos_table_backup.restore_cli.DefaultAzureCredential", Mock(return_value=credential)
    )
    blob_service = Mock()
    blob_constructor = Mock(return_value=blob_service)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.BlobServiceClient", blob_constructor)
    target_service = Mock()
    target_constructor = Mock(return_value=target_service)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.TableServiceClient", target_constructor)
    crypto_constructor = Mock()
    monkeypatch.setattr("cosmos_table_backup.restore_cli.CryptographyClient", crypto_constructor)
    monitor = Mock()
    monkeypatch.setattr("cosmos_table_backup.restore_cli.configure_monitor_export", monitor)
    runner = Mock()
    runner.run.return_value.to_json.return_value = '{"status":"succeeded"}'
    runner_constructor = Mock(return_value=runner)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.RestoreRunner", runner_constructor)

    assert main(["restore-test"]) == 0

    config = runner_constructor.call_args.args[0]
    assert config.backup_id is None
    assert config.backup_container_name == "backups"
    assert config.source_account_resource_id.endswith("/databaseaccounts/source")
    assert config.target_account_resource_id.endswith("/databaseaccounts/isolated")
    monitor.assert_called_once_with(credential, INFRA_ENV["APPLICATIONINSIGHTS_CONNECTION_STRING"])
    target_constructor.assert_called_once_with(
        INFRA_ENV["RESTORE_TARGET_TABLE_ENDPOINT"],
        credential=credential,
        audience="https://cosmos.azure.com",
    )
    crypto_factory = runner_constructor.call_args.args[3]
    versioned_id = f"{INFRA_ENV['KEY_VAULT_KEY_ID']}/version-123"
    crypto_factory(versioned_id)
    crypto_constructor.assert_called_once_with(versioned_id, credential)
    assert capsys.readouterr().out.strip() == '{"status":"succeeded"}'
