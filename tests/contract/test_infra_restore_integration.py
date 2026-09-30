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
        "cosmos_table_backup.restore_cli.ManagedIdentityCredential", Mock(return_value=credential)
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


def test_plan_dispatch_has_only_managed_identity_and_no_target_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in INFRA_ENV.items():
        monkeypatch.setenv(name, value)
    credential = Mock()
    identity = Mock(return_value=credential)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.ManagedIdentityCredential", identity)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.configure_monitor_export", Mock())
    monkeypatch.setattr("cosmos_table_backup.restore_cli.BlobServiceClient", Mock())
    target = Mock(side_effect=AssertionError("plan must not construct target client"))
    monkeypatch.setattr("cosmos_table_backup.restore_cli.TableServiceClient", target)
    runner = Mock()
    runner.plan.return_value = '{"private":"plan"}'
    monkeypatch.setattr("cosmos_table_backup.restore_cli.RestoreRunner", Mock(return_value=runner))
    assert main(["restore-test", "--plan"]) == 0
    identity.assert_called_once_with(client_id="restore-client-id")
    target.assert_not_called()
    runner.run.assert_not_called()
    from cosmos_table_backup.restore_cli import plan_frames

    assert capsys.readouterr().out.splitlines() == list(plan_frames('{"private":"plan"}'))
    runner.plan.side_effect = RuntimeError("authentication")
    assert main(["restore-test", "--plan"]) == 1
    assert capsys.readouterr().out == ""


def test_private_plan_never_configures_exporter_or_emits_sdk_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    for name, value in INFRA_ENV.items():
        monkeypatch.setenv(name, value)
    monitor = Mock(side_effect=AssertionError("private plan must not export telemetry"))
    normal_logging = Mock(
        side_effect=AssertionError("private plan must not configure normal logger")
    )
    monkeypatch.setattr("cosmos_table_backup.restore_cli.configure_monitor_export", monitor)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.configure_logging", normal_logging)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.ManagedIdentityCredential", Mock())
    http_logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
    http_logger.disabled = False
    http_logger.setLevel(logging.INFO)

    def noisy_client(*args: object, **kwargs: object) -> Mock:
        http_logger.info("HTTP SECRET entity-value")
        logging.getLogger("azure.data.tables").warning("HTTP SECRET table-name")
        return Mock()

    monkeypatch.setattr("cosmos_table_backup.restore_cli.BlobServiceClient", noisy_client)
    runner = Mock()
    runner.plan.return_value = '{"private":"plan"}'
    monkeypatch.setattr("cosmos_table_backup.restore_cli.RestoreRunner", Mock(return_value=runner))
    assert main(["restore-test", "--plan"]) == 0
    output = capsys.readouterr()
    assert output.out.startswith("restore.plan.part:")
    assert output.err == ""
    monitor.assert_not_called()
    normal_logging.assert_not_called()
    runner.plan.side_effect = RuntimeError("SECRET")
    assert main(["restore-test", "--plan"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert '"event":"restore.failed"' in output.err
    assert "SECRET" not in output.err


def test_data_only_silences_sdk_http_but_keeps_allowlisted_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    from cosmos_table_backup.telemetry import SafeLogger

    for name, value in INFRA_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("RESTORE_BACKUP_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("RESTORE_PREPARATION_JSON", "{}")
    monkeypatch.setattr("cosmos_table_backup.restore_cli.ManagedIdentityCredential", Mock())
    http_logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")

    def monitor(*args: object) -> None:
        http_logger.disabled = False
        http_logger.setLevel(logging.INFO)

    monkeypatch.setattr("cosmos_table_backup.restore_cli.configure_monitor_export", monitor)
    logger = logging.Logger("test-safe-restore", logging.INFO)
    logger.addHandler(logging.StreamHandler())
    monkeypatch.setattr(
        "cosmos_table_backup.restore_cli.configure_logging", lambda _: SafeLogger(logger)
    )
    monkeypatch.setattr("cosmos_table_backup.restore_cli.BlobServiceClient", Mock())
    monkeypatch.setattr("cosmos_table_backup.restore_cli.TableServiceClient", Mock())
    runner = Mock()

    def run() -> Mock:
        http_logger.info("HTTP SECRET entity-value")
        logging.getLogger("azure.data.tables").warning("HTTP SECRET table-name")
        SafeLogger(logger).emit(
            "restore.data_verified", table_count=2, entity_count=3, entity_value="SECRET"
        )
        report = Mock()
        report.to_json.return_value = '{"status":"data_verified_pending_table_set"}'
        return report

    runner.run.side_effect = run
    constructor = Mock(return_value=runner)
    monkeypatch.setattr("cosmos_table_backup.restore_cli.RestoreRunner", constructor)
    assert main(["restore-test", "--data-only"]) == 0
    assert constructor.call_args.args[0].data_only is True
    output = capsys.readouterr()
    assert '"event":"restore.data_verified"' in output.err
    assert '"table_count":2' in output.err
    assert '"entity_count":3' in output.err
    assert "SECRET" not in output.out + output.err
