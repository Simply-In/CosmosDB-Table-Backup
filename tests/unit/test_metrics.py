from unittest.mock import Mock

import pytest
from azure.core.paging import ItemPaged

from cosmos_table_backup.discovery import iter_entities
from cosmos_table_backup.metrics import METRIC_FIELDS, StageMetrics
from cosmos_table_backup.serialization import OrderIndependentDigest
from cosmos_table_backup.storage import BlockBlobWriter
from cosmos_table_backup.telemetry import SafeLogger


def test_timing_is_constant_space_and_includes_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 0.1, 0.2, 0.5])
    monkeypatch.setattr("cosmos_table_backup.metrics.perf_counter", lambda: next(clock))
    metrics = StageMetrics()
    with metrics.time("page_fetch_ms", "page_fetch_max_ms"):
        pass
    with pytest.raises(ValueError), metrics.time("page_fetch_ms", "page_fetch_max_ms"):
        raise ValueError("sensitive")
    assert metrics.values == pytest.approx({"page_fetch_ms": 400, "page_fetch_max_ms": 300})
    for _ in range(10000):
        metrics.add("page_count", 1)
    assert len(metrics.values) == 3
    total = StageMetrics()
    total.include(metrics)
    total.include(metrics)
    assert total.values["page_count"] == 20000
    assert total.values["page_fetch_max_ms"] == pytest.approx(300)
    summary = total.summary(2000, 10, 100)
    assert summary["entities_per_second"] == 5
    assert summary["encrypted_bytes_per_second"] == 50
    assert summary["plaintext_bytes_per_second"] == 0
    assert total.summary(0, 0, 0)["entities_per_second"] == 0


def test_disabled_measurements_do_not_read_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cosmos_table_backup.metrics.perf_counter", Mock(side_effect=AssertionError)
    )
    metrics = StageMetrics(enabled=False)
    with metrics.time("page_fetch_ms"):
        metrics.add("page_count", 1)
        metrics.maximum("page_fetch_max_ms", 10)
    assert metrics.values == {}


def test_real_sdk_pager_times_fetch_not_consumer_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr("cosmos_table_backup.metrics.perf_counter", lambda: now[0])

    def fetch(token):  # type: ignore[no-untyped-def]
        now[0] += 0.25
        return token

    def extract(token):  # type: ignore[no-untyped-def]
        return ("last" if token is None else None, [{"value": 1}] if token is None else [])

    table = Mock()
    table.query_entities.return_value = ItemPaged(fetch, extract)
    service = Mock()
    service.get_table_client.return_value = table
    metrics = StageMetrics()
    for _ in iter_entities(service, "approved", 2, metrics):
        now[0] += 10
    assert metrics.values["page_count"] == 2
    assert metrics.values["page_fetch_ms"] == 500
    assert metrics.values["page_fetch_max_ms"] == 250


def test_storage_success_counters_and_failed_stage_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter(range(20))
    monkeypatch.setattr("cosmos_table_backup.metrics.perf_counter", lambda: next(clock))
    metrics = StageMetrics()
    client = Mock()
    writer = BlockBlobWriter(client, 4, metrics)
    writer.write(b"12345")
    writer.commit()
    assert metrics.values["stage_block_count"] == 2
    assert metrics.values["stage_block_bytes"] == 5
    assert metrics.values["stage_block_ms"] == 2000
    assert metrics.values["blob_commit_ms"] == 1000
    client.commit_block_list.assert_called_once()
    assert client.commit_block_list.call_args.kwargs["if_none_match"] == "*"
    client.stage_block.side_effect = RuntimeError("secret")
    with pytest.raises(RuntimeError):
        BlockBlobWriter(client, 4, metrics).write(b"1234")
    assert metrics.values["stage_block_count"] == 2
    assert metrics.values["stage_block_ms"] == 3000


def test_digest_metrics_preserve_hash_and_cleanup(tmp_path) -> None:  # type: ignore[no-untyped-def]
    metrics = StageMetrics()
    records = [str(value).encode() for value in range(70)]
    with OrderIndependentDigest(100) as expected:
        for record in records:
            expected.update(record)
        expected_hash = expected.hexdigest()
    with OrderIndependentDigest(2, tmp_path, metrics) as actual:
        for record in reversed(records):
            actual.update(record)
        assert actual.hexdigest() == expected_hash
    assert metrics.values["digest_spill_count"] == 35
    assert metrics.values["digest_spill_bytes"] == 70 * 32
    assert metrics.values["digest_merge_write_bytes"] == 70 * 32
    assert metrics.values["digest_spill_ms"] >= 0
    assert metrics.values["digest_merge_ms"] >= 0
    assert list(tmp_path.iterdir()) == []


def test_numeric_metric_allowlist_drops_source_and_sdk_data() -> None:
    logger = Mock()
    metrics = StageMetrics()
    SafeLogger(logger).emit(
        "table_completed",
        **metrics.summary(1, 0, 0),
        PartitionKey="sensitive-partition",
        RowKey="sensitive-row",
        table_name="sensitive-table",
        response_headers={"Authorization": "secret"},
        wrapped_key="secret",
        connection_string="secret",
    )
    message = logger.info.call_args.args[0]
    assert "sensitive" not in message
    assert "secret" not in message
    assert all(f'"{field}"' in message for field in METRIC_FIELDS)
