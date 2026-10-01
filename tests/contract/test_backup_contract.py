import asyncio
import base64
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.exceptions import InvalidTag

from cosmos_table_backup.backup import BackupError, BackupRunner, _object_aad
from cosmos_table_backup.config import BackupConfig
from cosmos_table_backup.encryption import canonical_json, decrypt_object
from cosmos_table_backup.metrics import StageMetrics
from cosmos_table_backup.storage import StorageError
from cosmos_table_backup.telemetry import SafeLogger


@dataclass
class ListedTable:
    name: str


class Paged:
    def __init__(self, entities, fail: bool = False):  # type: ignore[no-untyped-def]
        self.entities = entities
        self.fail = fail

    async def _page(self, entities):  # type: ignore[no-untyped-def]
        for entity in entities:
            yield entity

    async def by_page(self):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("query failed")
        midpoint = len(self.entities) // 2
        yield self._page(self.entities[:midpoint])
        yield self._page(self.entities[midpoint:])


class Table:
    def __init__(self, entities, fail: bool = False):  # type: ignore[no-untyped-def]
        self.entities = entities
        self.fail = fail
        self.closed = False

    def query_entities(self, *, query_filter: str, results_per_page: int):  # type: ignore[no-untyped-def]
        assert query_filter == ""
        assert results_per_page == 2
        return Paged(self.entities, self.fail)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        self.closed = True


class Tables:
    def __init__(self, fail_query: bool = False) -> None:
        self.opened: list[str] = []
        self.fail_query = fail_query
        self.clients: list[Table] = []
        self.data = {
            "alpha": [{"PartitionKey": "a", "RowKey": "1", "value": 42}],
            "Cards": [{"PartitionKey": "c", "RowKey": "1"}],
            "cards": [{"PartitionKey": "forbidden", "RowKey": "secret"}],
        }

    async def list_tables(self):  # type: ignore[no-untyped-def]
        for name in reversed(self.data):
            yield ListedTable(name)

    def get_table_client(self, name: str) -> Table:
        self.opened.append(name)
        client = Table(self.data[name], self.fail_query and name == "alpha")
        self.clients.append(client)
        return client


class Blob:
    def __init__(self, name: str, owner: Container) -> None:
        self.name = name
        self.owner = owner
        self.blocks: dict[str, bytes] = {}

    async def stage_block(self, *, block_id: str, data: bytes, length: int) -> None:
        assert length == len(data)
        self.blocks[block_id] = data
        self.owner.events.append(("stage", self.name))

    async def commit_block_list(self, blocks, **kwargs):  # type: ignore[no-untyped-def]
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
    client.wrap_key = AsyncMock()
    client.wrap_key.return_value.encrypted_key = b"w" * 256
    return client


def test_complete_backup_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cosmos_table_backup.backup.generate_dek", lambda: b"d" * 32)
    tables = Tables()
    blobs = Container()
    key_client = crypto()
    raw_logger = Mock()
    backup_id = asyncio.run(
        BackupRunner(config(), tables, blobs, key_client, SafeLogger(raw_logger)).run()
    )

    assert tables.opened == ["Cards", "alpha"]
    assert all(client.closed for client in tables.clients)
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
        assert measurement["source_wait_ms"] >= 0
        assert measurement["source_backpressure_ms"] >= 0
        assert "table_name" not in measurement
    assert completed["entity_count"] == 2
    assert completed["page_count"] == 4
    for field in ("source_wait_ms", "source_backpressure_ms"):
        assert completed[field] == sum(item[field] for item in table_metrics)
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
        asyncio.run(BackupRunner(config(), Tables(), blobs, crypto(), SafeLogger(Mock())).run())
        results.append((blobs.objects, blobs.events))
    assert results[0] == results[1]


def test_local_residual_subtracts_source_wait_not_overlapping_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def untimed(self, *args):  # type: ignore[no-untyped-def]
        yield

    original_writer = BackupRunner._writer

    def writer(self, name, metrics):  # type: ignore[no-untyped-def]
        if "/tables/" in name:
            metrics.values.update(
                page_fetch_ms=1000, source_wait_ms=7, upload_wait_ms=3, blob_commit_ms=2
            )
        return original_writer(self, name, metrics)

    monkeypatch.setattr(StageMetrics, "time", untimed)
    monkeypatch.setattr(BackupRunner, "_writer", writer)
    monkeypatch.setattr(
        "cosmos_table_backup.backup.perf_counter", Mock(side_effect=[0.0, 1.0, 1.1, 2.0])
    )
    tables = Tables()
    tables.data = {"alpha": tables.data["alpha"]}
    logger = Mock()
    asyncio.run(BackupRunner(config(), tables, Container(), crypto(), SafeLogger(logger)).run())
    records = [json.loads(call.args[0]) for call in logger.info.call_args_list]
    table = next(record for record in records if record["event"] == "table_completed")
    assert table["duration_ms"] == pytest.approx(100)
    assert table["page_fetch_ms"] == 1000
    assert table["local_processing_ms"] == pytest.approx(88)


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

    backup_run = runner.run()
    with pytest.raises(BackupError) as error:
        asyncio.run(backup_run)

    cause = error.value.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert isinstance(cause.exceptions[0], StorageError)
    assert "committed-block limit" in str(cause.exceptions[0])
    assert all(kind == "stage" for kind, _ in blobs.events)
    assert all(name.endswith("tables/00000000.enc") for _, name in blobs.events)
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
    backup_run = runner.run()
    with pytest.raises(BackupError):
        asyncio.run(backup_run)
    assert not any(name.endswith("manifest.enc") for name in blobs.objects)
    assert not any(name.endswith("bootstrap.json") for name in blobs.objects)
    events = [call.args[0] for call in raw_logger.info.call_args_list]
    assert sum('"event":"backup.failed"' in event for event in events) == 1
    assert all('"event":"backup.completed"' not in event for event in events)


@pytest.mark.parametrize("failure", ["stage", "commit", "serialization", "digest"])
def test_async_pipeline_failure_never_creates_completion_marker(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    blobs = Container()
    tables = Tables()
    if failure in {"stage", "commit"}:
        method = "stage_block" if failure == "stage" else "commit_block_list"
        monkeypatch.setattr(Blob, method, AsyncMock(side_effect=RuntimeError("failed")))
    elif failure == "serialization":
        monkeypatch.setattr(
            "cosmos_table_backup.backup.encode_entity", Mock(side_effect=ValueError("failed"))
        )
    else:
        monkeypatch.setattr(
            "cosmos_table_backup.backup.OrderIndependentDigest.hexdigest",
            Mock(side_effect=RuntimeError("failed")),
        )
    logger = Mock()
    runner = BackupRunner(config(), tables, blobs, crypto(), SafeLogger(logger))
    backup_run = runner.run()
    with pytest.raises(BackupError):
        asyncio.run(backup_run)
    assert not blobs.objects
    assert all(client.closed for client in tables.clients)
    assert all(not name.endswith("manifest.enc") for name in blobs.blobs)
    messages = [json.loads(call.args[0]) for call in logger.info.call_args_list]
    assert messages[-1]["event"] == "backup.failed"
    assert all(item["event"] != "backup.completed" for item in messages)


def test_async_backup_cancellation_closes_table_and_uploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def stalled(*args, **kwargs):  # type: ignore[no-untyped-def]
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(Blob, "stage_block", stalled)
        tables = Tables()
        blobs = Container()
        logger = Mock()
        before = asyncio.all_tasks()
        task = asyncio.create_task(
            BackupRunner(config(), tables, blobs, crypto(), SafeLogger(logger)).run()
        )
        async with asyncio.timeout(5):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cancelled.is_set()
        assert all(client.closed for client in tables.clients)
        assert not blobs.objects
        assert asyncio.all_tasks() == before
        records = [json.loads(call.args[0]) for call in logger.info.call_args_list]
        assert records[-1]["event"] == "backup.failed"
        assert records[-1]["error_type"] == "CancelledError"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["source", "upload", "consumer", "cancel"])
def test_prefetch_and_upload_failures_cancel_the_other_stage(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def scenario() -> None:
        read_started = asyncio.Event()
        read_stopped = asyncio.Event()
        upload_started = asyncio.Event()
        upload_stopped = asyncio.Event()
        before = asyncio.all_tasks()

        async def pages(self):  # type: ignore[no-untyped-def]
            yield self._page(self.entities)
            read_started.set()
            try:
                if failure == "source":
                    await upload_started.wait()
                    raise RuntimeError("sensitive read failure")
                await asyncio.Event().wait()
            finally:
                read_stopped.set()

        async def stage(*args, **kwargs):  # type: ignore[no-untyped-def]
            upload_started.set()
            try:
                if failure == "upload":
                    raise RuntimeError("sensitive upload failure")
                await asyncio.Event().wait()
            finally:
                upload_stopped.set()

        monkeypatch.setattr(Paged, "by_page", pages)
        monkeypatch.setattr(Blob, "stage_block", stage)
        if failure == "consumer":
            monkeypatch.setattr(
                "cosmos_table_backup.backup.encode_entity",
                Mock(side_effect=ValueError("sensitive serialization failure")),
            )
        tables = Tables()
        tables.data = {"alpha": [{"PartitionKey": "p", "RowKey": "r", "value": "x" * 1024}]}
        blobs = Container()
        logger = Mock()
        task = asyncio.create_task(
            BackupRunner(config(), tables, blobs, crypto(), SafeLogger(logger)).run()
        )
        async with asyncio.timeout(5):
            if failure == "cancel":
                await read_started.wait()
                await upload_started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(BackupError):
                    await task
        assert read_started.is_set()
        assert read_stopped.is_set()
        if failure != "consumer":
            assert upload_started.is_set()
            assert upload_stopped.is_set()
        assert all(client.closed for client in tables.clients)
        assert not blobs.objects
        assert all(not name.endswith("manifest.enc") for name in blobs.blobs)
        assert asyncio.all_tasks() == before
        records = [json.loads(call.args[0]) for call in logger.info.call_args_list]
        assert records[-1]["event"] == "backup.failed"
        assert "sensitive" not in json.dumps(records)

    asyncio.run(scenario())
