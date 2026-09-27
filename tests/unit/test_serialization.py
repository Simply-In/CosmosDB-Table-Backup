import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from azure.data.tables import EdmType, EntityProperty

from cosmos_table_backup.serialization import (
    OrderIndependentDigest,
    SerializationError,
    decode_entity,
    encode_entity,
)


def test_golden_entity_all_supported_edm_types() -> None:
    entity = {
        "PartitionKey": "pk",
        "RowKey": "rk",
        "Timestamp": datetime(2025, 1, 2, 3, 4, 5, 6000, tzinfo=UTC),
        "bin": EntityProperty(b"\x00\xff", EdmType.BINARY),
        "bool": EntityProperty(True, EdmType.BOOLEAN),
        "double": EntityProperty(1.25, EdmType.DOUBLE),
        "guid": EntityProperty(UUID("12345678-1234-5678-1234-567812345678"), EdmType.GUID),
        "i32": EntityProperty(7, EdmType.INT32),
        "i64": EntityProperty(7, EdmType.INT64),
    }
    encoded = encode_entity(entity)
    assert encoded == (
        b'{"properties":{"PartitionKey":{"type":"String","value":"pk"},'
        b'"RowKey":{"type":"String","value":"rk"},"Timestamp":{"type":"DateTime",'
        b'"value":"2025-01-02T03:04:05.006000Z"},"bin":{"type":"Binary","value":"AP8="},'
        b'"bool":{"type":"Boolean","value":true},"double":{"type":"Double","value":1.25},'
        b'"guid":{"type":"Guid","value":"12345678-1234-5678-1234-567812345678"},'
        b'"i32":{"type":"Int32","value":7},"i64":{"type":"Int64","value":7}},"version":1}\n'
    )
    decoded = decode_entity(encoded)
    assert decoded["PartitionKey"] == "pk"
    assert decoded["RowKey"] == "rk"
    for key in entity.keys() - {"PartitionKey", "RowKey"}:
        value = entity[key]
        assert decoded[key] == EntityProperty(
            value.value if isinstance(value, EntityProperty) else value,
            value.edm_type if isinstance(value, EntityProperty) else decoded[key].edm_type,
        )
    assert decoded["i32"].edm_type == EdmType.INT32
    assert decoded["i64"].edm_type == EdmType.INT64


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**63, object()])
def test_rejects_values_without_lossless_json_representation(value: object) -> None:
    with pytest.raises(SerializationError):
        encode_entity({"x": value})


def test_order_independent_digest_is_stable_for_shuffled_records() -> None:
    forward = OrderIndependentDigest()
    reverse = OrderIndependentDigest()
    for record in (b"first", b"second", b"third"):
        forward.update(record)
    for record in (b"third", b"second", b"first"):
        reverse.update(record)
    assert forward.hexdigest() == reverse.hexdigest()


@pytest.mark.parametrize(
    "entity",
    [
        {"RowKey": "r"},
        {"PartitionKey": "p"},
        {"PartitionKey": 1, "RowKey": "r"},
        {"PartitionKey": "p", "RowKey": b"r"},
    ],
)
def test_decode_requires_string_table_keys(entity: dict[str, object]) -> None:
    encoded = encode_entity(entity)
    with pytest.raises(SerializationError, match=r"PartitionKey|RowKey"):
        decode_entity(encoded)


def test_digest_spills_with_bounded_memory_preserves_duplicates_and_cleans() -> None:
    scratch_root = Path("tests/.digest-test-scratch")
    scratch_root.mkdir(exist_ok=True)
    records = [f"record-{index % 17}".encode() for index in range(70)]
    expected = hashlib.sha256(
        b"".join(sorted(hashlib.sha256(record).digest() for record in records))
    ).hexdigest()
    digest = OrderIndependentDigest(max_digests_in_memory=2, scratch_directory=scratch_root)
    for record in reversed(records):
        digest.update(record)
        assert digest.in_memory_count < 2
    work_directory = digest.scratch_path
    assert digest.spilled
    assert work_directory is not None
    assert work_directory.exists()
    assert digest.hexdigest() == expected
    assert not work_directory.exists()
    scratch_root.rmdir()


def test_digest_context_cleans_spills_after_error() -> None:
    scratch_root = Path("tests/.digest-error-scratch")
    scratch_root.mkdir(exist_ok=True)
    work_directory: Path | None = None
    digest = OrderIndependentDigest(max_digests_in_memory=1, scratch_directory=scratch_root)

    def raise_after_spill() -> None:
        nonlocal work_directory
        with digest:
            digest.update(b"record")
            work_directory = digest.scratch_path
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        raise_after_spill()
    assert work_directory is not None
    assert not work_directory.exists()
    scratch_root.rmdir()


def test_rejects_naive_datetime_and_malformed_records() -> None:
    naive_datetime = datetime(2025, 1, 1)
    with pytest.raises(SerializationError):
        encode_entity({"x": naive_datetime})
    with pytest.raises(SerializationError):
        decode_entity(b"not json")
    with pytest.raises(SerializationError):
        decode_entity(b'{"version":2,"properties":{}}')
    with pytest.raises(SerializationError):
        decode_entity(b'{"version":1,"properties":{"x":{"type":"Unknown","value":1}}}')
