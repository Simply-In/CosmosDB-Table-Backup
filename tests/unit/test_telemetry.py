import json
from unittest.mock import Mock

import pytest

from cosmos_table_backup.metrics import METRIC_FIELDS
from cosmos_table_backup.telemetry import SafeLogger, configure_monitor_export, safe_error_fields

NUMERIC_FIELDS = sorted(
    METRIC_FIELDS | {"table_index", "table_count", "entity_count", "byte_count", "duration_ms"}
)


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
@pytest.mark.parametrize(
    "value",
    [
        {"Authorization": "credential"},
        ["credential"],
        "credential",
        None,
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_logger_drops_invalid_measurements(field: str, value: object) -> None:
    logger = Mock()
    SafeLogger(logger).emit("table_completed", backup_id="id", **{field: value})
    assert json.loads(logger.info.call_args.args[0]) == {
        "backup_id": "id",
        "event": "table_completed",
    }


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
@pytest.mark.parametrize("value", [0, 42, 0.0, 1.25])
def test_logger_preserves_numeric_measurements(field: str, value: int | float) -> None:
    logger = Mock()
    SafeLogger(logger).emit("table_completed", **{field: value})
    assert json.loads(logger.info.call_args.args[0]) == {"event": "table_completed", field: value}


def test_logger_drops_numeric_subclasses_without_serializing_them() -> None:
    class SensitiveFloat(float):
        pass

    logger = Mock()
    SafeLogger(logger).emit("table_completed", page_fetch_ms=SensitiveFloat(1.0))
    assert logger.info.call_args.args[0] == '{"event":"table_completed"}'


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
