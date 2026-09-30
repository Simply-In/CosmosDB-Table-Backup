#!/usr/bin/env python3
"""Offline synthetic backup benchmark; run with PYTHONPATH=src and the pinned environment.

No Azure clients, credentials, exporters, or network calls are constructed. The sink
checks create-only completion ordering but deliberately discards encrypted bytes;
this measures local processing, not Azure throughput, RU, retries, or recoverability.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import platform
import statistics
import sys
import tracemalloc
from collections.abc import Iterable, Sequence
from contextlib import chdir
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, sleep
from types import SimpleNamespace
from typing import Any

from azure.core.paging import ItemPaged
from azure.keyvault.keys.crypto import KeyWrapAlgorithm

from cosmos_table_backup import __version__
from cosmos_table_backup.backup import BackupRunner
from cosmos_table_backup.config import BackupConfig
from cosmos_table_backup.metrics import METRIC_FIELDS
from cosmos_table_backup.telemetry import SafeLogger

_NUMERIC_FIELDS = METRIC_FIELDS | {"duration_ms", "entity_count", "byte_count", "table_count"}


@dataclass(frozen=True, slots=True)
class Settings:
    entities: int = 70_000
    payload_bytes: int = 256
    partitions: int = 16
    page_size: int = 1000
    block_size: int = 4 * 1024 * 1024
    repeats: int = 3
    page_delay_ms: float = 0.0
    stage_delay_ms: float = 0.0
    trace_memory: bool = False

    def __post_init__(self) -> None:
        bounds = {
            "entities": (1, 1_000_000),
            "payload_bytes": (0, 64 * 1024),
            "partitions": (1, 1024),
            "page_size": (1, 1000),
            "block_size": (64 * 1024, 100 * 1024 * 1024),
            "repeats": (1, 10),
        }
        for name, (minimum, maximum) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        for name in ("page_delay_ms", "stage_delay_ms"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"{name} must be finite and between 0 and 100")


class SyntheticTable:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.generated_count = 0
        pattern = "0123456789abcdef"
        self.payload = (pattern * ((settings.payload_bytes + 15) // 16))[: settings.payload_bytes]

    def _entity(self, index: int) -> dict[str, object]:
        self.generated_count += 1
        return {
            "PartitionKey": f"p{index % self.settings.partitions:04d}",
            "RowKey": f"r{index:08d}",
            "payload": self.payload,
            "ordinal": index,
        }

    def query_entities(self, *, query_filter: str, results_per_page: int) -> ItemPaged:
        if query_filter != "" or results_per_page != self.settings.page_size:
            raise ValueError("unexpected synthetic query")

        def get_next(token: str | None) -> tuple[str | None, list[dict[str, object]]]:
            start = int(token) if token else 0
            if self.settings.page_delay_ms:
                sleep(self.settings.page_delay_ms / 1000)
            end = min(start + results_per_page, self.settings.entities)
            # Like an SDK response, only the current bounded page is materialized.
            page = [self._entity(index) for index in range(start, end)]
            return (str(end) if end < self.settings.entities else None, page)

        return ItemPaged(get_next, lambda response: response)


class SyntheticSource:
    def __init__(self, settings: Settings) -> None:
        self.table = SyntheticTable(settings)

    def list_tables(self) -> Iterable[dict[str, str]]:
        return ({"name": "synthetic"}, {"name": "cards"})

    def get_table_client(self, table_name: str) -> SyntheticTable:
        if table_name != "synthetic":
            raise ValueError("protected or unexpected table opened")
        return self.table


class DiscardBlob:
    def __init__(self, name: str, sink: DiscardSink) -> None:
        self.name = name
        self.sink = sink
        self.staged_count = 0
        self.committed = False

    def stage_block(self, *, block_id: str, data: bytes, length: int) -> None:
        if self.committed or length != len(data) or not block_id:
            raise ValueError("invalid synthetic block")
        if self.sink.stage_delay_ms:
            sleep(self.sink.stage_delay_ms / 1000)
        self.staged_count += 1
        self.sink.staged_bytes += length
        self.sink.stage_count += 1
        # Do not retain payloads, block IDs, or per-object/event histories.

    def commit_block_list(self, blocks: Sequence[Any], **kwargs: object) -> None:
        if self.committed or kwargs.get("if_none_match") != "*":
            raise ValueError("commit must be create-only")
        if len(blocks) != self.staged_count:
            raise ValueError("incomplete block list")
        expected = ("tables/00000000.enc", "bootstrap.json", "manifest.enc")
        if self.sink.commit_count >= len(expected) or not self.name.endswith(
            "/" + expected[self.sink.commit_count]
        ):
            raise ValueError("invalid completion ordering")
        self.committed = True
        self.sink.commit_count += 1
        self.sink.completion_committed = self.sink.commit_count == len(expected)


class DiscardSink:
    def __init__(self, stage_delay_ms: float = 0.0) -> None:
        self.stage_delay_ms = stage_delay_ms
        self.stage_count = 0
        self.staged_bytes = 0
        self.commit_count = 0
        self.completion_committed = False

    def get_blob_client(self, name: str) -> DiscardBlob:
        return DiscardBlob(name, self)


class SyntheticWrapClient:
    def wrap_key(self, algorithm: KeyWrapAlgorithm, key: bytes) -> SimpleNamespace:
        if algorithm != KeyWrapAlgorithm.rsa_oaep_256 or len(key) != 32:
            raise ValueError("unexpected wrapping request")
        # Only the envelope shape is simulated; this is not recoverable key wrapping.
        return SimpleNamespace(encrypted_key=b"synthetic-not-a-wrapped-key")


class LatestCompletion(logging.Handler):
    """Keep at most two numeric completion summaries, never a logging history."""

    def __init__(self) -> None:
        super().__init__()
        self.backup: dict[str, int | float] | None = None
        self.table: dict[str, int | float] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        fields = json.loads(record.getMessage())
        if fields.get("event") not in {"backup.completed", "table_completed"}:
            return
        summary = {
            key: value
            for key, value in fields.items()
            if key in _NUMERIC_FIELDS and type(value) in {int, float} and math.isfinite(value)
        }
        if fields["event"] == "backup.completed":
            self.backup = summary
        else:
            self.table = summary


def run_once(settings: Settings, *, instrumentation_enabled: bool) -> dict[str, object]:
    source = SyntheticSource(settings)
    sink = DiscardSink(settings.stage_delay_ms)
    completion = LatestCompletion()
    logger = logging.Logger("offline-benchmark", level=logging.INFO)
    logger.propagate = False
    logger.addHandler(completion)
    config = BackupConfig(
        table_endpoint="https://offline.invalid",
        storage_account_url="https://offline.invalid",
        container_name="synthetic",
        key_id="https://offline.invalid/keys/synthetic/version",
        page_size=settings.page_size,
        block_size=settings.block_size,
    )
    try:
        # Digest defaults are unchanged; their scratch stays under an owned cwd.
        with (
            TemporaryDirectory(prefix=".ctb-benchmark-", dir=Path.cwd()) as scratch,
            chdir(scratch),
        ):
            started = perf_counter()
            BackupRunner(
                config,
                source,
                sink,
                SyntheticWrapClient(),
                SafeLogger(logger),
                instrumentation_enabled=instrumentation_enabled,
            ).run()
            wall_ms = (perf_counter() - started) * 1000
            if (
                not sink.completion_committed
                or sink.commit_count != 3
                or completion.backup is None
                or completion.table is None
                or source.table.generated_count != settings.entities
                or completion.backup.get("entity_count") != settings.entities
            ):
                raise RuntimeError("synthetic backup did not complete")
            if any(Path.cwd().iterdir()):
                raise RuntimeError("digest scratch was not cleaned")
        return {
            "wall_duration_ms": wall_ms,
            "completion_marker_committed": True,
            "sink_stage_count": sink.stage_count,
            "sink_staged_bytes": sink.staged_bytes,
            "completion": completion.backup if instrumentation_enabled else None,
            "table_completion": completion.table if instrumentation_enabled else None,
        }
    finally:
        logger.removeHandler(completion)
        completion.close()


def rss_highwater() -> dict[str, object]:
    try:
        import resource
    except ImportError:
        return {"available": False}
    return {
        "available": True,
        "value": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "units": "bytes"
        if sys.platform == "darwin"
        else "KiB on Linux; platform-defined otherwise",
        "scope": "process lifetime high-water; includes imports and all measurement passes",
    }


def benchmark(settings: Settings) -> dict[str, object]:
    if tracemalloc.is_tracing():
        raise RuntimeError("tracemalloc must be disabled during overhead timing")
    disabled: list[float] = []
    enabled: list[dict[str, object]] = []
    for repeat in range(settings.repeats):
        # Alternate pair order to reduce systematic warm-cache/order bias.
        for instrumented in (False, True) if repeat % 2 == 0 else (True, False):
            result = run_once(settings, instrumentation_enabled=instrumented)
            if instrumented:
                enabled.append(result)
            else:
                disabled.append(float(result["wall_duration_ms"]))
    disabled_median = statistics.median(disabled)
    enabled_median = statistics.median(float(run["wall_duration_ms"]) for run in enabled)
    memory: dict[str, object] | None = None
    if settings.trace_memory:
        tracemalloc.start()
        try:
            run_once(settings, instrumentation_enabled=True)
            current, peak = tracemalloc.get_traced_memory()
            memory = {"current_bytes": current, "peak_bytes": peak, "separate_enabled_pass": True}
        finally:
            tracemalloc.stop()
    return {
        "schema_version": 1,
        "application_version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "machine": platform.machine(),
        "settings": asdict(settings),
        "workload": {
            "description": (
                "one table; lazy SDK pages; ASCII payload; unique rows; round-robin partitions"
            ),
            "table_count": 1,
            "digest_memory_limit": 32_768,
            "network_calls": 0,
            "sink_retains_payloads": False,
            "real_service_measurement": False,
        },
        "disabled_wall_duration_ms": disabled,
        "enabled_runs": enabled,
        "median_disabled_wall_duration_ms": disabled_median,
        "median_enabled_wall_duration_ms": enabled_median,
        "instrumentation_overhead_percent": (
            (enabled_median / disabled_median - 1) * 100 if disabled_median else 0.0
        ),
        "tracemalloc": memory,
        "rss_highwater": rss_highwater(),
    }


def parse_settings(argv: Sequence[str] | None = None) -> Settings:
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = Settings()
    for name in ("entities", "payload_bytes", "partitions", "page_size", "block_size", "repeats"):
        parser.add_argument(
            "--" + name.replace("_", "-"), type=int, default=getattr(defaults, name)
        )
    for name in ("page_delay_ms", "stage_delay_ms"):
        parser.add_argument("--" + name.replace("_", "-"), type=float, default=0.0)
    parser.add_argument("--tracemalloc", dest="trace_memory", action="store_true")
    try:
        return Settings(**vars(parser.parse_args(argv)))
    except ValueError as exc:
        parser.error(str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    settings = parse_settings(argv)
    try:
        report = benchmark(settings)
    except Exception as exc:
        # Exception messages/tracebacks can contain records or SDK details.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
