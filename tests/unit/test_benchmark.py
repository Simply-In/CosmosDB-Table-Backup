import importlib.util
import json
import logging
import sys
import tracemalloc
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from azure.core.paging import ItemPaged

from cosmos_table_backup.backup import BackupError
from cosmos_table_backup.serialization import OrderIndependentDigest
from cosmos_table_backup.telemetry import SafeLogger


@pytest.fixture(scope="module")
def benchmark_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "benchmark-backup.py"
    spec = importlib.util.spec_from_file_location("offline_backup_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def tiny_settings(module: ModuleType):  # type: ignore[no-untyped-def]
    return module.Settings(entities=7, payload_bytes=8, partitions=3, page_size=2, repeats=1)


def test_tiny_real_backup_counts_and_cleanup(benchmark_module: ModuleType) -> None:
    before = set(Path.cwd().iterdir())
    settings = tiny_settings(benchmark_module)
    report = benchmark_module.benchmark(settings)
    assert set(Path.cwd().iterdir()) == before
    assert report["workload"]["digest_memory_limit"] == 32_768
    assert report["workload"]["network_calls"] == 0
    assert report["median_disabled_wall_duration_ms"] > 0
    assert report["median_enabled_wall_duration_ms"] > 0
    assert len(report["disabled_wall_duration_ms"]) == len(report["enabled_runs"]) == 1
    enabled = report["enabled_runs"][0]
    assert enabled["completion_marker_committed"]
    assert enabled["completion"]["entity_count"] == 7
    assert enabled["table_completion"]["entity_count"] == 7
    assert enabled["completion"]["page_count"] == 4
    assert enabled["completion"]["plaintext_byte_count"] > 7 * 8
    assert enabled["completion"]["byte_count"] == enabled["completion"]["plaintext_byte_count"] + 33
    assert enabled["completion"]["stage_block_count"] == enabled["sink_stage_count"] == 3
    assert enabled["completion"]["stage_block_bytes"] == enabled["sink_staged_bytes"]
    assert enabled["completion"]["digest_spill_count"] == 0
    encoded = json.dumps(report)
    for forbidden in ("PartitionKey", "RowKey", "encrypted_key", "key_id", "backup_id", 'payload"'):
        assert forbidden not in encoded
    assert all(type(value) in {int, float} for value in enabled["completion"].values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entities", 0),
        ("entities", 1_000_001),
        ("entities", 1.5),
        ("payload_bytes", -1),
        ("payload_bytes", 65_537),
        ("partitions", 0),
        ("partitions", 1025),
        ("page_size", 0),
        ("page_size", 1001),
        ("block_size", 65_535),
        ("block_size", 100 * 1024 * 1024 + 1),
        ("repeats", 0),
        ("repeats", 11),
        ("page_delay_ms", -1),
        ("page_delay_ms", 101),
        ("stage_delay_ms", float("nan")),
        ("stage_delay_ms", float("inf")),
    ],
)
def test_bounded_settings(benchmark_module: ModuleType, field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        replace(tiny_settings(benchmark_module), **{field: value})


def test_cli_settings(benchmark_module: ModuleType) -> None:
    defaults = benchmark_module.parse_settings([])
    assert defaults.entities == 70_000
    assert defaults.payload_bytes == 256
    assert defaults.page_size == 1000
    assert defaults.block_size == 4 * 1024 * 1024
    assert defaults.partitions == 16
    assert defaults.repeats == 3
    assert not defaults.trace_memory
    settings = benchmark_module.parse_settings(
        ["--entities", "7", "--page-delay-ms", "0.5", "--stage-delay-ms", "1", "--tracemalloc"]
    )
    assert settings.entities == 7
    assert settings.page_delay_ms == 0.5
    assert settings.stage_delay_ms == 1
    assert settings.trace_memory
    with pytest.raises(SystemExit) as exc:
        benchmark_module.parse_settings(["--repeats", "11"])
    assert exc.value.code == 2


def test_source_generates_only_requested_sdk_page(benchmark_module: ModuleType) -> None:
    table = benchmark_module.SyntheticTable(tiny_settings(benchmark_module))
    items = table.query_entities(query_filter="", results_per_page=2)
    assert isinstance(items, ItemPaged)
    assert table.generated_count == 0
    pages = items.by_page()
    first = list(next(pages))
    assert table.generated_count == len(first) == 2
    assert len(first[0]["payload"].encode()) == 8
    entities = first + [entity for page in pages for entity in page]
    assert len(entities) == 7
    assert len({entity["RowKey"] for entity in entities}) == 7
    assert len({entity["PartitionKey"] for entity in entities}) == 3
    duplicate = benchmark_module.SyntheticTable(tiny_settings(benchmark_module))
    assert list(duplicate.query_entities(query_filter="", results_per_page=2)) == entities
    with pytest.raises(ValueError):
        benchmark_module.SyntheticSource(tiny_settings(benchmark_module)).get_table_client("cards")


def test_non_retaining_sink_and_create_only_commit(benchmark_module: ModuleType) -> None:
    sink = benchmark_module.DiscardSink()
    blob = sink.get_blob_client("backups/synthetic/tables/00000000.enc")
    for _ in range(20):
        blob.stage_block(block_id="synthetic", data=b"x" * 100, length=100)
    assert sink.staged_bytes == 2000
    assert sink.stage_count == 20
    assert not any(isinstance(value, (bytes, list, dict)) for value in vars(sink).values())
    assert not any(isinstance(value, (bytes, list, dict)) for value in vars(blob).values())
    with pytest.raises(ValueError, match="create-only"):
        blob.commit_block_list([None] * 20, if_none_match=None)
    blob.commit_block_list([None] * 20, if_none_match="*")
    with pytest.raises(ValueError, match="create-only"):
        blob.commit_block_list([None] * 20, if_none_match="*")
    marker = sink.get_blob_client("backups/synthetic/manifest.enc")
    with pytest.raises(ValueError, match="ordering"):
        marker.commit_block_list([], if_none_match="*")
    assert not sink.completion_committed


def test_latest_completion_has_bounded_safe_records(benchmark_module: ModuleType) -> None:
    handler = benchmark_module.LatestCompletion()
    logger = logging.Logger("bounded-test", level=logging.INFO)
    logger.addHandler(handler)
    safe = SafeLogger(logger)
    for index in range(100):
        safe.emit("table_completed", entity_count=index, backup_id="synthetic", payload="private")
        safe.emit("backup.completed", entity_count=index, backup_id="synthetic")
    assert handler.table == handler.backup == {"entity_count": 99}
    assert not hasattr(handler, "records")
    logger.removeHandler(handler)
    handler.close()


def test_failure_cleans_owned_scratch_after_digest_spill(
    benchmark_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = set(Path.cwd().iterdir())
    original_entity = benchmark_module.SyntheticTable._entity
    observed_scratch: list[Path] = []

    def failing_entity(self, index):  # type: ignore[no-untyped-def]
        if index == 4:
            observed_scratch.extend(Path.cwd().glob(".ctb-digests-*"))
            raise RuntimeError("synthetic source failed")
        return original_entity(self, index)

    def small_digest(**kwargs):  # type: ignore[no-untyped-def]
        return OrderIndependentDigest(max_digests_in_memory=2, **kwargs)

    monkeypatch.setattr(benchmark_module.SyntheticTable, "_entity", failing_entity)
    monkeypatch.setattr("cosmos_table_backup.backup.OrderIndependentDigest", small_digest)
    with pytest.raises(BackupError):
        benchmark_module.run_once(tiny_settings(benchmark_module), instrumentation_enabled=True)
    assert len(observed_scratch) == 2
    assert all(not path.exists() for path in observed_scratch)
    assert set(Path.cwd().iterdir()) == before


def test_missing_completion_prevents_success_output(
    benchmark_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = benchmark_module.DiscardBlob.commit_block_list

    def drop_marker(self, blocks, **kwargs):  # type: ignore[no-untyped-def]
        commit(self, blocks, **kwargs)
        self.sink.completion_committed = False

    monkeypatch.setattr(benchmark_module.DiscardBlob, "commit_block_list", drop_marker)
    assert benchmark_module.main(["--entities", "1", "--repeats", "1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "failed", "error_type": "RuntimeError"}


def test_cli_success_json_only(
    benchmark_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert benchmark_module.main(["--entities", "3", "--repeats", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["enabled_runs"][0]["completion"]["entity_count"] == 3


def test_alternating_pairs(benchmark_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    original_run = benchmark_module.run_once
    order: list[bool] = []

    def measured(settings, *, instrumentation_enabled):  # type: ignore[no-untyped-def]
        order.append(instrumentation_enabled)
        return original_run(settings, instrumentation_enabled=instrumentation_enabled)

    monkeypatch.setattr(benchmark_module, "run_once", measured)
    report = benchmark_module.benchmark(replace(tiny_settings(benchmark_module), repeats=2))
    assert order == [False, True, True, False]
    assert len(report["enabled_runs"]) == len(report["disabled_wall_duration_ms"]) == 2


def test_tracemalloc_separate_pass(
    benchmark_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = benchmark_module.run_once
    tracing: list[bool] = []

    def measured(settings, **kwargs):  # type: ignore[no-untyped-def]
        tracing.append(tracemalloc.is_tracing())
        return original_run(settings, **kwargs)

    monkeypatch.setattr(benchmark_module, "run_once", measured)
    report = benchmark_module.benchmark(replace(tiny_settings(benchmark_module), trace_memory=True))
    assert tracing == [False, False, True]
    assert report["tracemalloc"]["peak_bytes"] > 0
    assert report["tracemalloc"]["separate_enabled_pass"]
    assert not tracemalloc.is_tracing()
