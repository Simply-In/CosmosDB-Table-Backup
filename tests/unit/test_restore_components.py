from unittest.mock import Mock

import pytest
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.pipeline.transport import HttpTransport
from azure.data.tables import EdmType, EntityProperty, TableClient, UpdateMode

from cosmos_table_backup.restore import EntityBatchWriter, JsonLineDecoder, RestoreError
from cosmos_table_backup.serialization import decode_entity, encode_entity


class RequestReachedTransport(RuntimeError):
    pass


class CaptureTransport(HttpTransport):
    def __init__(self) -> None:
        self.body_lengths: list[int] = []

    def __enter__(self):  # type: ignore[no-untyped-def]
        self.open()
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        del args
        self.close()

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        body = request.body
        assert isinstance(body, bytes)
        self.body_lengths.append(len(body))
        raise RequestReachedTransport


class SdkTable:
    def __init__(self) -> None:
        self.transport = CaptureTransport()
        self.client = TableClient(
            endpoint="https://example.table.core.windows.net",
            table_name="restore",
            credential=AzureNamedKeyCredential("account", "YQ=="),
            transport=self.transport,
        )

    def submit_transaction(self, operations):  # type: ignore[no-untyped-def]
        with pytest.raises(RequestReachedTransport):
            self.client.submit_transaction(operations)

    def upsert_entity(self, entity, *, mode):  # type: ignore[no-untyped-def]
        with pytest.raises(RequestReachedTransport):
            self.client.upsert_entity(entity, mode=mode)


class Table:
    def __init__(self) -> None:
        self.batches = []
        self.singles = []

    def submit_transaction(self, operations):  # type: ignore[no-untyped-def]
        self.batches.append(operations)

    def upsert_entity(self, entity, *, mode):  # type: ignore[no-untyped-def]
        self.singles.append((entity, mode))


def entity(partition: str, row: str):  # type: ignore[no-untyped-def]
    return {
        "PartitionKey": EntityProperty(partition, EdmType.STRING),
        "RowKey": EntityProperty(row, EdmType.STRING),
    }


def test_batching_is_bounded_partition_safe_and_idempotent_upsert() -> None:
    table = Table()
    writer = EntityBatchWriter(table, 2, 64 * 1024)
    for value in [entity("a", "1"), entity("a", "2"), entity("a", "3"), entity("b", "1")]:
        writer.add(value)
        assert writer.max_buffered <= 2
    writer.flush()
    assert [len(batch) for batch in table.batches] == [2, 1, 1]
    assert all(operation[0] == "upsert" for batch in table.batches for operation in batch)
    assert all(
        operation[2] == {"mode": UpdateMode.REPLACE}
        for batch in table.batches
        for operation in batch
    )


def test_batcher_requires_keys() -> None:
    writer = EntityBatchWriter(Mock(), 1, 64 * 1024)
    with pytest.raises(RestoreError):
        writer.add({"PartitionKey": "p"})


def test_batching_never_exceeds_100_operations() -> None:
    table = Table()
    writer = EntityBatchWriter(table, 1000, 1_500_000)
    for index in range(101):
        writer.add(entity("a", str(index)))
    writer.flush()
    assert [len(batch) for batch in table.batches] == [100, 1]


def test_batching_respects_conservative_payload_bound() -> None:
    table = Table()
    entities = [{**entity("a", row), "value": "x" * 500} for row in ("1", "2")]
    ceiling = EntityBatchWriter.BATCH_FRAMING_BYTES + max(
        EntityBatchWriter.estimated_operation_bytes(item) for item in entities
    )
    writer = EntityBatchWriter(table, 100, ceiling)
    for item in entities:
        writer.add(item)
    writer.flush()
    assert [len(batch) for batch in table.batches] == [1, 1]
    assert writer.max_buffered_bytes <= ceiling
    assert table.singles == []


def test_decoded_entity_reaches_real_sdk_request_construction() -> None:
    table = SdkTable()
    restored = decode_entity(
        encode_entity({"PartitionKey": "partition", "RowKey": "row", "value": "ok"})
    )
    table.submit_transaction([("upsert", restored, {"mode": UpdateMode.REPLACE})])
    assert isinstance(restored["PartitionKey"], str)
    assert isinstance(restored["RowKey"], str)
    assert table.transport.body_lengths


def test_unicode_batches_fit_estimate_and_real_sdk_request_limit() -> None:
    table = SdkTable()
    writer = EntityBatchWriter(table, 100, 1_500_000)
    for index in range(100):
        writer.add(
            {
                "PartitionKey": "分区😀",
                "RowKey": f"行😀{index}",
                "value": EntityProperty("𐀀漢😀" * 1000, EdmType.STRING),
            }
        )
    writer.flush()
    assert writer.max_buffered_bytes <= 1_500_000
    assert len(table.transport.body_lengths) > 1
    assert max(table.transport.body_lengths) < 2 * 1024 * 1024
    assert max(table.transport.body_lengths) <= writer.max_buffered_bytes


def test_batch_payload_ceiling_cannot_exceed_1_5_mb() -> None:
    table = Table()
    with pytest.raises(RestoreError, match=r"at most 1\.5 MB"):
        EntityBatchWriter(table, 100, 1_500_001)


def test_oversized_entity_is_submitted_individually() -> None:
    table = Table()
    writer = EntityBatchWriter(table, 100, 6000)
    oversized = {**entity("a", "large"), "value": "x" * 2000}
    writer.add(oversized)
    writer.flush()
    assert table.batches == []
    assert table.singles == [(oversized, UpdateMode.REPLACE)]
    assert writer.entity_count == 1


def test_json_lines_are_streamed_with_a_strict_bound() -> None:
    records = []
    decoder = JsonLineDecoder(5, records.append)
    decoder.write(b"one\nt")
    assert decoder.buffered_bytes == 1
    decoder.write(b"wo\n")
    decoder.finalize()
    assert records == [b"one", b"two"]
    bounded_decoder = JsonLineDecoder(2, lambda _: None)
    with pytest.raises(RestoreError):
        bounded_decoder.write(b"toolong")
    incomplete = JsonLineDecoder(10, lambda _: None)
    incomplete.write(b"partial")
    with pytest.raises(RestoreError):
        incomplete.finalize()


def test_create_only_batch_and_oversized_single_use_real_sdk() -> None:
    table = SdkTable()
    table.client.create_entity = Mock(side_effect=RequestReachedTransport)  # type: ignore[method-assign]
    table.create_entity = table.client.create_entity  # type: ignore[attr-defined]
    writer = EntityBatchWriter(table, 2, 64 * 1024, create_only=True)
    writer.add({"PartitionKey": "p", "RowKey": "r"})
    writer.flush()
    assert table.transport.body_lengths
    single = Mock()
    writer = EntityBatchWriter(single, 2, 6000, create_only=True)
    oversized = {"PartitionKey": "p", "RowKey": "r", "value": "x" * 2000}
    writer.add(oversized)
    writer.flush()
    single.create_entity.assert_called_once_with(oversized)
    single.upsert_entity.assert_not_called()
    single.submit_transaction.assert_not_called()
