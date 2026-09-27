import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import EdmType, EntityProperty
from cryptography.exceptions import InvalidTag

from cosmos_table_backup.backup import _object_aad
from cosmos_table_backup.encryption import ObjectEncryptor, canonical_json, make_bootstrap
from cosmos_table_backup.manifest import BackupManifest, TableManifest
from cosmos_table_backup.restore import RestoreError, RestoreRunner
from cosmos_table_backup.restore_config import RestoreConfig
from cosmos_table_backup.serialization import (
    OrderIndependentDigest,
    encode_entity,
    encode_entity_content,
    encode_entity_key,
)
from cosmos_table_backup.telemetry import SafeLogger

BACKUP_ID = "11111111-1111-4111-8111-111111111111"
DEK = b"d" * 32


class Sink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)


def encrypt(plaintext: bytes, nonce: bytes, aad: bytes) -> bytes:
    sink = Sink()
    encryptor = ObjectEncryptor(DEK, nonce, aad, sink)
    encryptor.write(plaintext)
    encryptor.finalize()
    return bytes(sink.data)


def unordered_hash(records: list[bytes]) -> str:
    digest = OrderIndependentDigest()
    for record in records:
        digest.update(record)
    return digest.hexdigest()


class Source:
    def __init__(self, objects: dict[str, bytes], marker: bool = True, chunk_size: int = 7) -> None:
        self.objects = objects
        self.marker = marker
        self.chunk_size = chunk_size
        self.reads: list[str] = []

    def successful_backup_ids(self) -> set[str]:
        return {BACKUP_ID} if self.marker else set()

    def latest_successful_backup_id(self) -> str:
        if not self.marker:
            raise RuntimeError("no completed backup")
        return BACKUP_ID

    def snapshot(self, name: str) -> str:
        if name not in self.objects:
            raise RuntimeError("missing")
        return "etag"

    def chunks(self, name: str, etag: str):  # type: ignore[no-untyped-def]
        assert etag == "etag"
        self.reads.append(name)
        data = self.objects[name]
        for offset in range(0, len(data), self.chunk_size):
            yield data[offset : offset + self.chunk_size]

    def read_limited(self, name: str, limit: int) -> tuple[bytes, str]:
        data = self.objects[name]
        if len(data) > limit:
            raise RuntimeError("limit")
        self.reads.append(name)
        return data, "etag"


class TargetTable:
    def __init__(self) -> None:
        self.rows: dict[tuple[object, object], dict[str, object]] = {}
        self.batch_sizes: list[int] = []

    def _upsert(self, entity):  # type: ignore[no-untyped-def]
        partition = entity["PartitionKey"]
        row = entity["RowKey"]
        assert isinstance(partition, str)
        assert isinstance(row, str)
        self.rows[(partition, row)] = dict(entity)

    def submit_transaction(self, operations):  # type: ignore[no-untyped-def]
        self.batch_sizes.append(len(operations))
        for operation, entity, options in operations:
            assert operation == "upsert"
            assert str(options["mode"]) == "UpdateMode.REPLACE"
            self._upsert(entity)

    def upsert_entity(self, entity, *, mode):  # type: ignore[no-untyped-def]
        assert str(mode) == "UpdateMode.REPLACE"
        self._upsert(entity)

    def query_entities(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs == {"query_filter": ""}
        return [row for _, row in sorted(self.rows.items())]


class Target:
    def __init__(self) -> None:
        self.tables: dict[str, TargetTable] = {}
        self.deleted: list[str] = []

    def list_tables(self):  # type: ignore[no-untyped-def]
        return [SimpleNamespace(name=name) for name in sorted(self.tables)]

    def delete_table(self, name: str) -> None:
        if name not in self.tables:
            raise ResourceNotFoundError("missing")
        self.deleted.append(name)
        del self.tables[name]

    def create_table(self, name: str) -> TargetTable:
        table = TargetTable()
        self.tables[name] = table
        return table


class ShuffledQueryTarget(Target):
    def create_table(self, name: str) -> TargetTable:
        table = super().create_table(name)
        original_query = table.query_entities

        def query_reversed(**kwargs):  # type: ignore[no-untyped-def]
            return list(reversed(original_query(**kwargs)))

        table.query_entities = query_reversed  # type: ignore[method-assign]
        return table


class LateExtraTableTarget(Target):
    def __init__(self) -> None:
        super().__init__()
        self.list_calls = 0

    def list_tables(self):  # type: ignore[no-untyped-def]
        self.list_calls += 1
        if self.list_calls == 3:
            self.tables["LateLegacy"] = TargetTable()
        return super().list_tables()


class ResidualTableTarget(Target):
    def delete_table(self, name: str) -> None:
        if name == "Legacy":
            return
        super().delete_table(name)


class ExtraKeyTarget(Target):
    def create_table(self, name: str) -> TargetTable:
        table = super().create_table(name)
        original_query = table.query_entities

        def query_with_extra(**kwargs):  # type: ignore[no-untyped-def]
            return [
                *original_query(**kwargs),
                {"PartitionKey": "unexpected", "RowKey": "entity"},
            ]

        table.query_entities = query_with_extra  # type: ignore[method-assign]
        return table


def config(batch_size: int = 2) -> RestoreConfig:
    return RestoreConfig(
        source_account_resource_id="/subscriptions/s/resourceGroups/r/providers/Microsoft.DocumentDB/databaseAccounts/source",
        backup_storage_account_url="https://backup.blob.core.windows.net",
        backup_container_name="backups",
        target_account_resource_id="/subscriptions/s/resourceGroups/r/providers/Microsoft.DocumentDB/databaseAccounts/isolated",
        target_table_endpoint="https://isolated.table.cosmos.azure.com",
        expected_key_id="https://vault.vault.azure.net/keys/key",
        backup_id=BACKUP_ID,
        managed_identity_client_id="restore-id",
        application_insights_connection_string="InstrumentationKey=test",
        application_insights_authentication_string="Authorization=AAD;ClientId=restore-id",
        batch_size=batch_size,
        max_record_bytes=64 * 1024,
        max_manifest_bytes=64 * 1024,
        download_chunk_size=64 * 1024,
    )


def fixture(entity_count: int = 3) -> tuple[dict[str, bytes], list[dict[str, object]]]:
    entities: list[dict[str, object]] = []
    for index in range(entity_count):
        entities.append(
            {
                "PartitionKey": EntityProperty("partition", EdmType.STRING),
                "RowKey": EntityProperty(str(index), EdmType.STRING),
                "Timestamp": EntityProperty(datetime(2025, 1, 1, tzinfo=UTC), EdmType.DATETIME),
                "binary": EntityProperty(b"\x00\xff", EdmType.BINARY),
                "boolean": EntityProperty(index % 2 == 0, EdmType.BOOLEAN),
                "double": EntityProperty(1.25, EdmType.DOUBLE),
                "guid": EntityProperty(UUID("12345678-1234-5678-1234-567812345678"), EdmType.GUID),
                "int32": EntityProperty(7, EdmType.INT32),
                "int64": EntityProperty(7, EdmType.INT64),
                "string": EntityProperty("value", EdmType.STRING),
            }
        )
    plaintext = b"".join(encode_entity(entity) for entity in entities)
    key_records = [encode_entity_key(entity) for entity in entities]
    content_records = [encode_entity_content(entity) for entity in entities]
    table_object = encrypt(plaintext, b"t" * 12, _object_aad(BACKUP_ID, "table", 0))
    table = TableManifest(
        table_name="Restored",
        object_name="tables/00000000.enc",
        entity_count=len(entities),
        encrypted_byte_count=len(table_object),
        encrypted_sha256=hashlib.sha256(table_object).hexdigest(),
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        keys_sha256=unordered_hash(key_records),
        entity_content_sha256=unordered_hash(content_records),
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:01:00Z",
    )
    manifest = BackupManifest(
        backup_id=BACKUP_ID,
        application_version="0.1.0",
        manifest_version=1,
        consistency="per-table streaming export; no cross-table point-in-time guarantee",
        started_at="2025-01-01T00:00:00Z",
        completed_at="2025-01-01T00:01:00Z",
        tables=(table,),
    )
    bootstrap = make_bootstrap(
        BACKUP_ID,
        "https://vault.vault.azure.net/keys/key/version",
        b"wrapped",
        b"m" * 12,
    )
    bootstrap_bytes = canonical_json(bootstrap)
    manifest_object = encrypt(manifest.to_bytes(), b"m" * 12, bootstrap_bytes)
    prefix = f"backups/{BACKUP_ID}"
    return {
        f"{prefix}/bootstrap.json": bootstrap_bytes,
        f"{prefix}/manifest.enc": manifest_object,
        f"{prefix}/tables/00000000.enc": table_object,
    }, entities


def crypto_factory(client: Mock):  # type: ignore[no-untyped-def]
    def factory(key_id: str) -> Mock:
        assert key_id == "https://vault.vault.azure.net/keys/key/version"
        return client

    return factory


def test_authenticated_restore_type_fidelity_bounded_batches_and_idempotency() -> None:
    objects, original = fixture(5)
    source = Source(objects)
    target = Target()
    stale = TargetTable()
    stale.rows[("stale", "row")] = {
        "PartitionKey": EntityProperty("stale", EdmType.STRING),
        "RowKey": EntityProperty("row", EdmType.STRING),
    }
    target.tables["Restored"] = stale
    target.tables["Legacy"] = TargetTable()
    crypto = Mock()
    crypto.unwrap_key.return_value = SimpleNamespace(key=DEK)
    raw_logger = Mock()
    runner = RestoreRunner(
        replace(config(2), backup_id=None),
        source,
        target,
        crypto_factory(crypto),
        SafeLogger(raw_logger),
    )

    first = runner.run()
    second = runner.run()

    assert first.to_json() == second.to_json()
    assert first.entity_count == 5
    assert first.deterministic_hash == second.deterministic_hash
    assert len(target.tables["Restored"].rows) == 5
    assert target.tables["Restored"].batch_sizes == [2, 2, 1]
    assert target.deleted == ["Legacy", "Restored", "Restored"]
    assert set(target.tables) == {"Restored"}
    assert ("stale", "row") not in target.tables["Restored"].rows
    assert first.tables[0].target_keys_sha256 == unordered_hash(
        [encode_entity_key(entity) for entity in original]
    )
    assert first.tables[0].target_content_sha256 == unordered_hash(
        [encode_entity_content(entity) for entity in original]
    )
    restored = target.tables["Restored"].rows[("partition", "0")]
    assert restored["PartitionKey"] == "partition"
    assert restored["RowKey"] == "0"
    for name, value in original[0].items():
        if name in {"PartitionKey", "RowKey"}:
            continue
        assert restored[name].edm_type == value.edm_type  # type: ignore[union-attr]
        assert restored[name].value == value.value  # type: ignore[union-attr]
    assert crypto.unwrap_key.call_count == 2
    table_path = f"backups/{BACKUP_ID}/tables/00000000.enc"
    assert source.reads.count(table_path) == 4
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"restore.started"' in event for event in events) == 2
    assert sum('"event":"restore.completed"' in event for event in events) == 2
    assert all('"event":"restore.failed"' not in event for event in events)


def test_target_query_order_does_not_change_verification_hashes() -> None:
    objects, original = fixture(5)
    crypto = Mock()
    crypto.unwrap_key.return_value = SimpleNamespace(key=DEK)
    report = RestoreRunner(
        config(),
        Source(objects),
        ShuffledQueryTarget(),
        crypto_factory(crypto),
        SafeLogger(Mock()),
    ).run()
    assert report.tables[0].target_keys_sha256 == unordered_hash(
        [encode_entity_key(entity) for entity in original]
    )
    assert report.tables[0].target_content_sha256 == unordered_hash(
        [encode_entity_content(entity) for entity in original]
    )


def test_table_appearing_during_restore_is_removed_before_completion() -> None:
    objects, _ = fixture()
    target = LateExtraTableTarget()
    crypto = Mock()
    crypto.unwrap_key.return_value = SimpleNamespace(key=DEK)
    RestoreRunner(
        config(), Source(objects), target, crypto_factory(crypto), SafeLogger(Mock())
    ).run()
    assert target.deleted == ["LateLegacy"]
    assert set(target.tables) == {"Restored"}


def test_unexpected_target_table_must_be_deleted_and_absent() -> None:
    objects, _ = fixture()
    target = ResidualTableTarget()
    target.tables["Legacy"] = TargetTable()
    crypto = Mock()
    crypto.unwrap_key.return_value = SimpleNamespace(key=DEK)
    raw_logger = Mock()
    runner = RestoreRunner(
        config(), Source(objects), target, crypto_factory(crypto), SafeLogger(raw_logger)
    )
    with pytest.raises(RestoreError, match="unexpected target tables remain"):
        runner.run()
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert any('"event":"restore.failed"' in event for event in events)
    assert all('"event":"restore.completed"' not in event for event in events)


def test_actual_target_key_mismatch_fails_before_completion() -> None:
    objects, _ = fixture()
    crypto = Mock()
    crypto.unwrap_key.return_value = SimpleNamespace(key=DEK)
    raw_logger = Mock()
    runner = RestoreRunner(
        config(),
        Source(objects),
        ExtraKeyTarget(),
        crypto_factory(crypto),
        SafeLogger(raw_logger),
    )
    with pytest.raises(RestoreError, match="target table content"):
        runner.run()
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"restore.failed"' in event for event in events) == 1
    assert all('"event":"restore.completed"' not in event for event in events)


@pytest.mark.parametrize("object_suffix", ["manifest.enc", "tables/00000000.enc"])
def test_tampering_fails_closed_before_success_report(object_suffix: str) -> None:
    objects, _ = fixture()
    path = f"backups/{BACKUP_ID}/{object_suffix}"
    changed = bytearray(objects[path])
    changed[-1] ^= 1
    objects[path] = bytes(changed)
    target = Target()
    crypto = Mock()
    crypto.unwrap_key.return_value = SimpleNamespace(key=DEK)
    raw_logger = Mock()
    runner = RestoreRunner(
        config(), Source(objects), target, crypto_factory(crypto), SafeLogger(raw_logger)
    )
    with pytest.raises((RestoreError, InvalidTag)):
        runner.run()
    assert target.tables == {}
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"restore.failed"' in event for event in events) == 1
    assert all('"event":"restore.completed"' not in event for event in events)


def test_partial_run_without_manifest_is_never_read_or_restored() -> None:
    objects, _ = fixture()
    source = Source(objects, marker=False)
    target = Target()
    factory = Mock()
    runner = RestoreRunner(config(), source, target, factory, SafeLogger(Mock()))
    with pytest.raises(RestoreError, match="no committed manifest"):
        runner.run()
    assert source.reads == []
    assert target.tables == {}
    factory.assert_not_called()
