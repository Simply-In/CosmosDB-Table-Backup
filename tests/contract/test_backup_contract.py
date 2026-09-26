import base64
import hashlib
import json
from dataclasses import dataclass
from unittest.mock import Mock

import pytest
from cryptography.exceptions import InvalidTag

from cosmos_table_backup.backup import BackupError, BackupRunner, _object_aad
from cosmos_table_backup.config import BackupConfig
from cosmos_table_backup.encryption import canonical_json, decrypt_object
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
    def __init__(self, name: str, owner: "Container") -> None:
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

    changed_bootstrap = {**bootstrap, "key_id": "https://attacker.invalid/keys/k/v"}
    with pytest.raises(InvalidTag):
        decrypt_object(b"d" * 32, encoded_manifest, canonical_json(changed_bootstrap))


def test_any_read_failure_leaves_no_completion_marker() -> None:
    blobs = Container()
    raw_logger = Mock()
    with pytest.raises(BackupError):
        BackupRunner(
            config(), Tables(fail_query=True), blobs, crypto(), SafeLogger(raw_logger)
        ).run()
    assert not any(name.endswith("manifest.enc") for name in blobs.objects)
    assert not any(name.endswith("bootstrap.json") for name in blobs.objects)
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"backup.failed"' in event for event in events) == 1
    assert all('"event":"backup.completed"' not in event for event in events)
