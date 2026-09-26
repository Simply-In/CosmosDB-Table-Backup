from unittest.mock import Mock

import pytest

from cosmos_table_backup.telemetry import SafeLogger, configure_monitor_export, safe_error_fields


def test_logger_drops_non_allowlisted_sensitive_fields() -> None:
    logger = Mock()
    SafeLogger(logger).emit("failure", backup_id="id", entity="secret", token="credential")
    message = logger.info.call_args.args[0]
    assert message == '{"backup_id":"id","event":"failure"}'
    assert "secret" not in message
    assert safe_error_fields(ValueError("secret")) == {
        "status": "failed",
        "error_type": "ValueError",
    }


def test_monitor_export_uses_managed_identity_and_app_insights_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure = Mock()
    monkeypatch.setattr("cosmos_table_backup.telemetry.configure_azure_monitor", configure)
    credential = Mock()
    configure_monitor_export(credential, "InstrumentationKey=test")
    configure.assert_called_once_with(
        connection_string="InstrumentationKey=test",
        credential=credential,
        logger_name="cosmos_table_backup",
        enable_live_metrics=False,
    )
