"""Structured, allowlisted telemetry without entity data."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from typing import Any, Final

from azure.monitor.opentelemetry import configure_azure_monitor

from cosmos_table_backup.metrics import METRIC_FIELDS

_NUMERIC_FIELDS: Final = METRIC_FIELDS | frozenset(
    {"table_index", "table_count", "entity_count", "byte_count", "duration_ms"}
)
_ALLOWED_FIELDS: Final = _NUMERIC_FIELDS | frozenset({"event", "backup_id", "status", "error_type"})


class SafeLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(self, event: str, **fields: object) -> None:
        record: dict[str, object] = {"event": event}
        for key, value in fields.items():
            if key not in _ALLOWED_FIELDS:
                continue
            if key in _NUMERIC_FIELDS and not (
                type(value) is int or (type(value) is float and math.isfinite(value))
            ):
                continue
            record[key] = value
        self._logger.info(json.dumps(record, separators=(",", ":"), sort_keys=True))


def configure_logging(level: str) -> SafeLogger:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(message)s")
    return SafeLogger(logging.getLogger("cosmos_table_backup"))


def silence_sdk_logging() -> None:
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


def configure_monitor_export(credential: Any, connection_string: str) -> None:
    configure_azure_monitor(
        connection_string=connection_string,
        credential=credential,
        logger_name="cosmos_table_backup",
        enable_live_metrics=False,
    )


def safe_error_fields(exc: BaseException) -> Mapping[str, str]:
    return {"status": "failed", "error_type": type(exc).__name__}
