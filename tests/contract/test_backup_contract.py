import base64
import hashlib
import json
from dataclasses import dataclass, replace
from unittest.mock import Mock

import pytest
from cryptography.exceptions import InvalidTag

from cosmos_table_backup.backup import BackupError, BackupRunner, _object_aad
from cosmos_table_backup.config import BackupConfig
from cosmos_table_backup.encryption import canonical_json, decrypt_object
from cosmos_table_backup.storage import StorageError
from cosmos_table_backup.telemetry import SafeLogger


@dataclass
class ListedTable:
    name: str


class Paged:
    def __init__(self, entities, fail: bool = False):  # type: ignore[no-untyped-def]
        self.entities = entities
        self.fail = fail

    def by_page(self):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("query failed")
        midpoint = len(self.entities) // 2
        yield self.entities[:midpoint]
        yield self.entities[midpoint:]


class Table:
    def __init__(self, entities, fail: bool = False):  # type: ignore[no-untyped-def]
        self.entities = entities
        self.fail = fail

    def query_entities(self, *, query_filter: str, results_per_page: int):  # type: ignore[no-untyped-def]
        assert query_filter == ""
        assert results_per_page == 2
        return Paged(self.entities, self.fail)


class Tables:
    def __init__(self, fail_query: bool = False) -> None:
        self.opened: list[str] = []
        self.fail_query = fail_query
        self.data = {
            "alpha": [{"PartitionKey": "a", "RowKey": "1", "value": 42}],
            "Cards": [{"PartitionKey": "c", "RowKey": "1"}],
            "cards": [{"PartitionKey": "forbidden", "RowKey": "secret"}],
        }

    def list_tables(self):  # type: ignore[no-untyped-def]
        return [ListedTable(name) for name in reversed(self.data)]

    def get_table_client(self, name: str) -> Table:
        self.opened.append(name)
        return Table(self.data[name], self.fail_query and name == "alpha")


class Blob:
    def __init__(self, name: str, owner: Container) -> None:
        self.name = name
        self.owner = owner
        self.blocks: dict[str, bytes] = {}

    def stage_block(self, *, block_id: str, data: bytes, length: int) -> None:
        assert length == len(data)
        self.blocks[block_id] = data
        self.owner.events.append(("stage", self.name))

    def commit_block_list(self, blocks, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["if_none_match"] == "*"
        if self.name in self.owner.objects:
            raise RuntimeError("overwrite")
        self.owner.objects[self.name] = b"".join(self.blocks[block.id] for block in blocks)
        self.owner.events.append(("commit", self.name))


class Container:
    def __init__(self) -> None:
        self.blobs: dict[str, Blob] = {}
        self.objects: dict[str, bytes] = {}
        self.events: list[tuple[str, str]] = []

    def get_blob_client(self, name: str) -> Blob:
        return self.blobs.setdefault(name, Blob(name, self))


def config() -> BackupConfig:
    return BackupConfig(
        table_endpoint="https://source.table.cosmos.azure.com",
        storage_account_url="https://backup.blob.core.windows.net",
        container_name="backups",
        key_id="https://vault.vault.azure.net/keys/key/version",
        page_size=2,
        block_size=64,
    )


def crypto() -> Mock:
    client = Mock()
    client.wrap_key.return_value.encrypted_key = b"w" * 256
    return client


def test_complete_backup_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cosmos_table_backup.backup.generate_dek", lambda: b"d" * 32)
    tables = Tables()
    blobs = Container()
    key_client = crypto()
    raw_logger = Mock()
    backup_id = BackupRunner(config(), tables, blobs, key_client, SafeLogger(raw_logger)).run()

    assert tables.opened == ["Cards", "alpha"]
    assert "cards" not in tables.opened
    assert key_client.wrap_key.call_count == 1
    prefix = f"backups/{backup_id}/"
    assert set(blobs.objects) == {
        prefix + "tables/00000000.enc",
        prefix + "tables/00000001.enc",
        prefix + "bootstrap.json",
        prefix + "manifest.enc",
    }
    assert blobs.events[-1] == ("commit", prefix + "manifest.enc")

    bootstrap_bytes = blobs.objects[prefix + "bootstrap.json"]
    bootstrap = json.loads(bootstrap_bytes)
    assert "tables" not in bootstrap
    assert bootstrap["backup_id"] == backup_id
    nonce = base64.b64decode(bootstrap["manifest_nonce"])
    encoded_manifest = blobs.objects[prefix + "manifest.enc"]
    assert encoded_manifest[5:17] == nonce
    manifest = json.loads(decrypt_object(b"d" * 32, encoded_manifest, bootstrap_bytes))
    assert [item["table_name"] for item in manifest["tables"]] == ["Cards", "alpha"]
    assert manifest["consistency"].startswith("per-table streaming")

    for index, table in enumerate(manifest["tables"]):
        encoded = blobs.objects[prefix + table["object_name"]]
        plaintext = decrypt_object(b"d" * 32, encoded, _object_aad(backup_id, "table", index))
        assert plaintext.count(b"\n") == table["entity_count"]
        assert hashlib.sha256(plaintext).hexdigest() == table["plaintext_sha256"]
        assert len(table["keys_sha256"]) == 64

    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"backup.completed"' in event for event in events) == 1
    assert all('"event":"backup.failed"' not in event for event in events)
    records = [json.loads(event) for event in events]
    table_metrics = [event for event in records if event["event"] == "table_completed"]
    completed = next(event for event in records if event["event"] == "backup.completed")
    for index, measurement in enumerate(table_metrics):
        encrypted = blobs.objects[prefix + f"tables/{index:08d}.enc"]
        assert measurement["byte_count"] == len(encrypted)
        assert measurement["plaintext_byte_count"] == len(encrypted) - 33
        assert measurement["page_count"] == 2
        assert measurement["stage_block_bytes"] == len(encrypted)
        assert measurement["duration_ms"] > 0
        assert measurement["entities_per_second"] > 0
        assert measurement["plaintext_bytes_per_second"] > 0
        assert measurement["digest_spill_count"] == 0
        assert measurement["digest_merge_ms"] >= 0
        assert "table_name" not in measurement
    assert completed["entity_count"] == 2
    assert completed["page_count"] == 4
    assert completed["byte_count"] == sum(item["byte_count"] for item in table_metrics)
    assert completed["stage_block_bytes"] == sum(map(len, blobs.objects.values()))
    assert completed["stage_block_count"] == sum(kind == "stage" for kind, _ in blobs.events)
    assert completed["duration_ms"] >= sum(item["duration_ms"] for item in table_metrics)

    changed_bootstrap = {**bootstrap, "key_id": "https://attacker.invalid/keys/k/v"}
    changed_bootstrap_bytes = canonical_json(changed_bootstrap)
    with pytest.raises(InvalidTag):
        decrypt_object(b"d" * 32, encoded_manifest, changed_bootstrap_bytes)


def test_telemetry_timing_does_not_change_backup_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cosmos_table_backup.backup.uuid4", lambda: "fixed-test-run")
    monkeypatch.setattr("cosmos_table_backup.backup._now", lambda: "2026-01-01T00:00:00Z")
    monkeypatch.setattr("cosmos_table_backup.backup.generate_dek", lambda: b"d" * 32)
    monkeypatch.setattr(
        "cosmos_table_backup.backup.NonceFactory",
        lambda: Mock(generate=Mock(side_effect=[b"m" * 12, b"a" * 12, b"b" * 12])),
    )
    results = []
    for clock_step in (0.001, 1.0):
        monkeypatch.setattr(
            "cosmos_table_backup.metrics.perf_counter",
            Mock(side_effect=[tick * clock_step for tick in range(1000)]),
        )
        blobs = Container()
        BackupRunner(config(), Tables(), blobs, crypto(), SafeLogger(Mock())).run()
        results.append((blobs.objects, blobs.events))
    assert results[0] == results[1]


@pytest.mark.parametrize("block_size", [16, 32])
def test_block_overflow_leaves_no_completion_marker(
    monkeypatch: pytest.MonkeyPatch, block_size: int
) -> None:
    monkeypatch.setattr("cosmos_table_backup.storage.MAX_COMMITTED_BLOCKS", 1)
    tables = Tables()
    tables.data = {"alpha": []}
    blobs = Container()
    raw_logger = Mock()
    runner = BackupRunner(
        replace(config(), block_size=block_size),
        tables,
        blobs,
        crypto(),
        SafeLogger(raw_logger),
    )

    with pytest.raises(BackupError) as error:
        runner.run()

    assert isinstance(error.value.__cause__, StorageError)
    assert "committed-block limit" in str(error.value.__cause__)
    assert len(blobs.events) == 1
    assert blobs.events[0][0] == "stage"
    assert blobs.events[0][1].endswith("tables/00000000.enc")
    assert not blobs.objects
    assert not any(name.endswith("manifest.enc") for name in blobs.blobs)
    assert not any(name.endswith("bootstrap.json") for name in blobs.blobs)
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"backup.failed"' in event for event in events) == 1
    assert all('"event":"backup.completed"' not in event for event in events)


def test_any_read_failure_leaves_no_completion_marker() -> None:
    blobs = Container()
    raw_logger = Mock()
    runner = BackupRunner(
        config(), Tables(fail_query=True), blobs, crypto(), SafeLogger(raw_logger)
    )
    with pytest.raises(BackupError):
        runner.run()
    assert not any(name.endswith("manifest.enc") for name in blobs.objects)
    assert not any(name.endswith("bootstrap.json") for name in blobs.objects)
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"backup.failed"' in event for event in events) == 1
    assert all('"event":"backup.completed"' not in event for event in events)
