"""Authenticated, bounded and idempotent restore orchestration."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import EntityProperty, UpdateMode

from cosmos_table_backup.backup import _object_aad
from cosmos_table_backup.encryption import (
    canonical_json,
    decrypt_chunks,
    decrypt_object,
    unwrap_dek,
)
from cosmos_table_backup.restore_config import RestoreConfig
from cosmos_table_backup.serialization import (
    OrderIndependentDigest,
    decode_entity,
    encode_entity_ascii,
    encode_entity_content,
    encode_entity_key,
)
from cosmos_table_backup.telemetry import SafeLogger


class RestoreError(RuntimeError):
    """Raised when restore cannot be fully authenticated and completed."""


class RestoreSource(Protocol):
    def successful_backup_ids(self) -> set[str]: ...
    def latest_successful_backup_id(self) -> str: ...
    def read_limited(self, name: str, limit: int) -> tuple[bytes, str]: ...
    def snapshot(self, name: str) -> str: ...
    def chunks(self, name: str, etag: str) -> Iterable[bytes]: ...


@dataclass(frozen=True, slots=True)
class TableVerification:
    table_name: str
    entity_count: int
    encrypted_byte_count: int
    encrypted_sha256: str
    plaintext_sha256: str
    target_keys_sha256: str
    target_content_sha256: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    report_version: int
    backup_id: str
    target_endpoint: str
    table_count: int
    entity_count: int
    tables: tuple[TableVerification, ...]
    deterministic_hash: str
    status: str = "succeeded"
    table_set_verified: bool = True

    def to_json(self) -> str:
        value = asdict(self)
        if self.report_version == 1:
            del value["table_set_verified"]
        return canonical_json(value).decode("utf-8")


class JsonLineDecoder:
    """Decode arbitrarily chunked JSON Lines with a strict record-size bound."""

    def __init__(self, max_record_bytes: int, consume: Callable[[bytes], None]) -> None:
        self._max = max_record_bytes
        self._consume = consume
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > self._max:
                    raise RestoreError("entity record exceeds configured size limit")
                return
            if newline > self._max:
                raise RestoreError("entity record exceeds configured size limit")
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if not line:
                raise RestoreError("empty entity record")
            self._consume(line)

    def finalize(self) -> None:
        if self._buffer:
            raise RestoreError("entity stream does not end at a record boundary")


class EntityBatchWriter:
    """Submit partition-safe writes bounded by operation count and estimated wire bytes."""

    MAX_PAYLOAD_BYTES = 1_500_000
    BATCH_FRAMING_BYTES = 2048
    OPERATION_FRAMING_BYTES = 2048

    def __init__(
        self,
        table_client: Any,
        batch_size: int,
        max_payload_bytes: int,
        *,
        create_only: bool = False,
    ) -> None:
        if not 0 < max_payload_bytes <= self.MAX_PAYLOAD_BYTES:
            raise RestoreError("transaction payload ceiling must be at most 1.5 MB")
        self._table = table_client
        self._create_only = create_only
        self._limit = min(batch_size, 100)
        self._max_payload_bytes = max_payload_bytes
        self._partition: object | None = None
        self._entities: list[Mapping[str, Any]] = []
        self._payload_bytes = self.BATCH_FRAMING_BYTES
        self.entity_count = 0
        self.max_buffered = 0
        self.max_buffered_bytes = 0

    @classmethod
    def estimated_operation_bytes(cls, entity: Mapping[str, Any]) -> int:
        # ASCII escaping bounds JSON Unicode expansion; the factor covers OData annotations,
        # URL-escaped keys, multipart headers, and SDK serialization differences.
        return len(encode_entity_ascii(entity)) * 2 + cls.OPERATION_FRAMING_BYTES

    def add(self, entity: Mapping[str, Any]) -> None:
        if "PartitionKey" not in entity or "RowKey" not in entity:
            raise RestoreError("entity is missing PartitionKey or RowKey")
        partition = entity["PartitionKey"]
        if isinstance(partition, EntityProperty):
            partition = partition.value
        estimated_bytes = self.estimated_operation_bytes(entity)
        if self._entities and (
            partition != self._partition
            or len(self._entities) >= self._limit
            or self._payload_bytes + estimated_bytes > self._max_payload_bytes
        ):
            self.flush()
        if self.BATCH_FRAMING_BYTES + estimated_bytes > self._max_payload_bytes:
            if self._create_only:
                self._table.create_entity(entity)
            else:
                self._table.upsert_entity(entity, mode=UpdateMode.REPLACE)
        else:
            self._partition = partition
            self._entities.append(entity)
            self._payload_bytes += estimated_bytes
            self.max_buffered = max(self.max_buffered, len(self._entities))
            self.max_buffered_bytes = max(self.max_buffered_bytes, self._payload_bytes)
        self.entity_count += 1

    def flush(self) -> None:
        if not self._entities:
            return
        operations = (
            [("create", entity, {}) for entity in self._entities]
            if self._create_only
            else [("upsert", entity, {"mode": UpdateMode.REPLACE}) for entity in self._entities]
        )
        self._table.submit_transaction(operations)
        self._entities.clear()
        self._payload_bytes = self.BATCH_FRAMING_BYTES
        self._partition = None


def _parse_json(data: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreError(f"invalid {description}") from exc
    if not isinstance(value, dict):
        raise RestoreError(f"invalid {description}")
    return value


def _validate_bootstrap(
    data: bytes, backup_id: str, expected_key_id: str
) -> tuple[dict[str, Any], bytes, bytes]:
    bootstrap = _parse_json(data, "bootstrap")
    required = {
        "backup_id",
        "bootstrap_version",
        "encrypted_manifest",
        "key_id",
        "manifest_nonce",
        "object_format",
        "wrap_algorithm",
        "wrapped_dek",
    }
    if set(bootstrap) != required or canonical_json(bootstrap) != data:
        raise RestoreError("bootstrap schema or canonical encoding is invalid")
    if (
        bootstrap["backup_id"] != backup_id
        or bootstrap["bootstrap_version"] != 1
        or bootstrap["object_format"] != 1
        or bootstrap["encrypted_manifest"] != "manifest.enc"
        or bootstrap["wrap_algorithm"] != "RSA-OAEP-256"
    ):
        raise RestoreError("unsupported or mismatched bootstrap")
    key_id = bootstrap["key_id"]
    if (
        not isinstance(key_id, str)
        or key_id.count("/keys/") != 1
        or key_id.rsplit("/", 1)[0].rstrip("/") != expected_key_id.rstrip("/")
        or len(key_id.rsplit("/", 1)[-1]) == 0
    ):
        raise RestoreError("bootstrap key ID is not an allowed exact key version")
    try:
        wrapped = base64.b64decode(bootstrap["wrapped_dek"], validate=True)
        nonce = base64.b64decode(bootstrap["manifest_nonce"], validate=True)
    except Exception as exc:
        raise RestoreError("bootstrap contains invalid base64") from exc
    if len(nonce) != 12 or not wrapped:
        raise RestoreError("bootstrap key material metadata is invalid")
    return bootstrap, wrapped, nonce


def _validate_manifest(data: bytes, backup_id: str) -> dict[str, Any]:
    manifest = _parse_json(data, "manifest")
    if manifest.get("backup_id") != backup_id or manifest.get("manifest_version") != 1:
        raise RestoreError("manifest identity or version is invalid")
    tables = manifest.get("tables")
    if not isinstance(tables, list) or not tables:
        raise RestoreError("manifest contains no tables")
    names: set[str] = set()
    objects: set[str] = set()
    for item in tables:
        if not isinstance(item, dict):
            raise RestoreError("invalid table manifest")
        name = item.get("table_name")
        object_name = item.get("object_name")
        hashes = (
            item.get("encrypted_sha256"),
            item.get("plaintext_sha256"),
            item.get("keys_sha256"),
            item.get("entity_content_sha256"),
        )
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or not isinstance(object_name, str)
            or object_name in objects
            or object_name != f"tables/{len(objects):08d}.enc"
            or not isinstance(item.get("entity_count"), int)
            or item["entity_count"] < 0
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hashes
            )
        ):
            raise RestoreError("invalid or duplicate table manifest entry")
        names.add(name)
        objects.add(object_name)
    return manifest


def _report_hash(tables: list[TableVerification]) -> str:
    return hashlib.sha256(canonical_json([asdict(table) for table in tables])).hexdigest()


def _listed_table_name(table: object) -> str:
    name = table.get("name") if isinstance(table, Mapping) else getattr(table, "name", None)
    if not isinstance(name, str) or not name:
        raise RestoreError("target table enumeration returned an invalid name")
    return name


class RestoreRunner:
    def __init__(
        self,
        config: RestoreConfig,
        source: RestoreSource,
        target_service: Any,
        crypto_client_factory: Callable[[str], Any],
        logger: SafeLogger,
    ) -> None:
        self._config = config
        self._source = source
        self._target = target_service
        self._crypto_factory = crypto_client_factory
        self._log = logger

    def _authenticated_manifest(self) -> tuple[str, bytes, dict[str, Any], bytes]:
        backup_id = self._config.backup_id
        if backup_id is None:
            backup_id = self._source.latest_successful_backup_id()
        elif backup_id not in self._source.successful_backup_ids():
            raise RestoreError("requested backup has no committed manifest")
        prefix = f"backups/{backup_id}"
        bootstrap_bytes, _ = self._source.read_limited(
            f"{prefix}/bootstrap.json", self._config.max_manifest_bytes
        )
        bootstrap, wrapped_dek, manifest_nonce = _validate_bootstrap(
            bootstrap_bytes, backup_id, self._config.expected_key_id
        )
        dek = unwrap_dek(self._crypto_factory(str(bootstrap["key_id"])), wrapped_dek)
        encrypted_manifest, _ = self._source.read_limited(
            f"{prefix}/manifest.enc", self._config.max_manifest_bytes
        )
        try:
            manifest_bytes = decrypt_object(dek, encrypted_manifest, bootstrap_bytes)
        except Exception as exc:
            raise RestoreError("manifest authentication failed") from exc
        if encrypted_manifest[5:17] != manifest_nonce:
            raise RestoreError("manifest nonce does not match bootstrap")
        return backup_id, dek, _validate_manifest(manifest_bytes, backup_id), manifest_bytes

    def _plan(
        self, backup_id: str, manifest: dict[str, Any], manifest_bytes: bytes
    ) -> dict[str, Any]:
        names = sorted(str(item["table_name"]) for item in manifest["tables"])
        if len(names) > 100:
            raise RestoreError("preparation plan exceeds the 100-table bound")
        if any(
            not 3 <= len(name) <= 63
            or not name.isascii()
            or not name[0].isalpha()
            or not name.isalnum()
            for name in names
        ):
            raise RestoreError("plan contains an invalid Azure Table name")
        plan = {
            "schema_version": 1,
            "backup_id": backup_id,
            "source_account_resource_id": self._config.source_account_resource_id,
            "target_account_resource_id": self._config.target_account_resource_id,
            "target_table_endpoint": self._config.target_table_endpoint,
            "backup_storage_account_url": self._config.backup_storage_account_url,
            "backup_container_name": self._config.backup_container_name,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "table_names": names,
        }
        if len(canonical_json(plan)) > min(16 * 1024, self._config.max_manifest_bytes):
            raise RestoreError("preparation plan exceeds the manifest byte bound")
        return plan

    def plan(self) -> str:
        """Return private operator JSON; never enumerate or mutate target resources."""
        backup_id, dek, manifest, manifest_bytes = self._authenticated_manifest()
        plan = self._plan(backup_id, manifest, manifest_bytes)
        for index, item in enumerate(manifest["tables"]):
            self._authenticate_table(backup_id, f"backups/{backup_id}", index, item, dek)
        return canonical_json(plan).decode("utf-8")

    def _prepared_tables(self, plan: dict[str, Any]) -> dict[str, Any]:
        assertion = self._config.preparation_json
        if assertion is None:
            raise RestoreError("data-only restore requires a governed preparation assertion")
        if len(assertion.encode("utf-8")) > min(16 * 1024, self._config.max_manifest_bytes):
            raise RestoreError("preparation assertion exceeds the manifest byte bound")
        prepared = _parse_json(assertion.encode("utf-8"), "preparation assertion")
        if canonical_json(prepared) != canonical_json(plan):
            raise RestoreError("preparation assertion does not match authenticated backup binding")
        tables: dict[str, Any] = {}
        for name in plan["table_names"]:
            client = self._target.get_table_client(name)
            if (
                next(iter(client.query_entities(query_filter="", results_per_page=1)), None)
                is not None
            ):
                raise RestoreError("prepared target table is not empty")
            tables[name] = client
        return tables

    def run(self) -> VerificationReport:
        backup_id = self._config.backup_id
        try:
            self._log.emit("restore.started", backup_id=backup_id)
            backup_id, dek, manifest, manifest_bytes = self._authenticated_manifest()
            prefix = f"backups/{backup_id}"
            expected_tables = {str(item["table_name"]) for item in manifest["tables"]}
            prepared: dict[str, Any] = {}
            snapshots: list[tuple[str, str, str, int]] = []
            if self._config.data_only:
                prepared = self._prepared_tables(self._plan(backup_id, manifest, manifest_bytes))
                # Authenticate every table against pinned ETags before the first entity mutation.
                for index, item in enumerate(manifest["tables"]):
                    snapshots.append(self._authenticate_table(backup_id, prefix, index, item, dek))
            else:
                self._remove_unexpected_tables(expected_tables)
            reports: list[TableVerification] = []
            for index, item in enumerate(manifest["tables"]):
                reports.append(
                    self._restore_table(
                        backup_id,
                        prefix,
                        index,
                        item,
                        dek,
                        prepared.get(str(item["table_name"])),
                        snapshots[index] if self._config.data_only else None,
                    )
                )
            if not self._config.data_only:
                self._remove_unexpected_tables(expected_tables)
                if self._target_table_names() != expected_tables:
                    raise RestoreError("target table set does not exactly match manifest")
            report = VerificationReport(
                report_version=2 if self._config.data_only else 1,
                backup_id=backup_id,
                target_endpoint=self._config.target_table_endpoint,
                table_count=len(reports),
                entity_count=sum(item.entity_count for item in reports),
                tables=tuple(reports),
                deterministic_hash=_report_hash(reports),
                status="data_verified_pending_table_set" if self._config.data_only else "succeeded",
                table_set_verified=not self._config.data_only,
            )
            self._log.emit(
                "restore.data_verified" if self._config.data_only else "restore.completed",
                backup_id=backup_id,
                table_count=report.table_count,
                entity_count=report.entity_count,
                status=report.status,
            )
            return report
        except Exception as exc:
            self._log.emit(
                "restore.failed",
                backup_id=backup_id,
                status="failed",
                error_type=type(exc).__name__,
            )
            if isinstance(exc, RestoreError):
                raise
            raise RestoreError("restore failed") from exc

    def _target_table_names(self) -> set[str]:
        names = [_listed_table_name(item) for item in self._target.list_tables()]
        if len(names) != len(set(names)):
            raise RestoreError("target table enumeration returned duplicate names")
        return set(names)

    def _remove_unexpected_tables(self, expected_tables: set[str]) -> None:
        for table_name in sorted(self._target_table_names() - expected_tables):
            self._target.delete_table(table_name)
        if not self._target_table_names() <= expected_tables:
            raise RestoreError("unexpected target tables remain after cleanup")

    def _authenticate_table(
        self, backup_id: str, prefix: str, index: int, item: Mapping[str, Any], dek: bytes
    ) -> tuple[str, str, str, int]:
        object_name = f"{prefix}/{item['object_name']}"
        etag = self._source.snapshot(object_name)
        aad = _object_aad(backup_id, "table", index)
        encrypted_hash, plaintext_hash, byte_count = decrypt_chunks(
            dek, self._source.chunks(object_name, etag), aad, lambda _: None
        )
        if (
            encrypted_hash != item.get("encrypted_sha256")
            or plaintext_hash != item.get("plaintext_sha256")
            or byte_count != item.get("encrypted_byte_count")
        ):
            raise RestoreError("table object does not match manifest")
        return etag, encrypted_hash, plaintext_hash, byte_count

    def _restore_table(
        self,
        backup_id: str,
        prefix: str,
        index: int,
        item: Mapping[str, Any],
        dek: bytes,
        table_client: Any = None,
        snapshot: tuple[str, str, str, int] | None = None,
    ) -> TableVerification:
        object_name = f"{prefix}/{item['object_name']}"
        aad = _object_aad(backup_id, "table", index)
        etag, encrypted_hash, plaintext_hash, byte_count = snapshot or self._authenticate_table(
            backup_id, prefix, index, item, dek
        )
        table_name = str(item["table_name"])
        if self._config.data_only:
            if table_client is None or snapshot is None:
                raise RestoreError("data-only table is not authenticated and prepared")
        else:
            with suppress(ResourceNotFoundError):
                self._target.delete_table(table_name)
            table_client = self._target.create_table(table_name)
        batcher = EntityBatchWriter(
            table_client,
            self._config.batch_size,
            self._config.max_batch_payload_bytes,
            create_only=self._config.data_only,
        )
        decoder = JsonLineDecoder(
            self._config.max_record_bytes,
            lambda line: batcher.add(decode_entity(line)),
        )
        second_encrypted_hash, second_plaintext_hash, second_byte_count = decrypt_chunks(
            dek, self._source.chunks(object_name, etag), aad, decoder.write
        )
        decoder.finalize()
        batcher.flush()
        if (
            second_encrypted_hash != encrypted_hash
            or second_plaintext_hash != plaintext_hash
            or second_byte_count != byte_count
            or batcher.entity_count != item.get("entity_count")
        ):
            raise RestoreError("restored table verification failed")
        target_count = 0
        with (
            OrderIndependentDigest() as target_keys_hash,
            OrderIndependentDigest() as target_content_hash,
        ):
            for entity in table_client.query_entities(query_filter=""):
                target_keys_hash.update(encode_entity_key(entity))
                target_content_hash.update(encode_entity_content(entity))
                target_count += 1
            target_keys_sha256 = target_keys_hash.hexdigest()
            target_content_sha256 = target_content_hash.hexdigest()
        if (
            target_count != item["entity_count"]
            or target_keys_sha256 != item["keys_sha256"]
            or target_content_sha256 != item["entity_content_sha256"]
        ):
            raise RestoreError("target table content does not match manifest")
        return TableVerification(
            table_name=table_name,
            entity_count=target_count,
            encrypted_byte_count=byte_count,
            encrypted_sha256=encrypted_hash,
            plaintext_sha256=plaintext_hash,
            target_keys_sha256=target_keys_sha256,
            target_content_sha256=target_content_sha256,
        )
