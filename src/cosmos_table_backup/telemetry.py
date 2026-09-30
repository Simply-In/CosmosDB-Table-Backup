"""Structured, allowlisted telemetry without entity data."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Final

from azure.monitor.opentelemetry import configure_azure_monitor

from cosmos_table_backup.metrics import METRIC_FIELDS

_ALLOWED_FIELDS: Final = (
    frozenset(
        {
            "event",
            "backup_id",
            "table_index",
            "table_count",
            "entity_count",
            "byte_count",
            "duration_ms",
            "status",
            "error_type",
        }
    )
    | METRIC_FIELDS
)


class SafeLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(self, event: str, **fields: object) -> None:
        record: dict[str, object] = {"event": event}
        record.update({key: value for key, value in fields.items() if key in _ALLOWED_FIELDS})
        self._logger.info(json.dumps(record, separators=(",", ":"), sort_keys=True))


def configure_logging(level: str) -> SafeLogger:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(message)s")
    return SafeLogger(logging.getLogger("cosmos_table_backup"))


def configure_monitor_export(credential: Any, connection_string: str) -> None:
    configure_azure_monitor(
        connection_string=connection_string,
        credential=credential,
        logger_name="cosmos_table_backup",
        enable_live_metrics=False,
    )


def safe_error_fields(exc: BaseException) -> Mapping[str, str]:
    return {"status": "failed", "error_type": type(exc).__name__}
