"""Fail-closed orchestration for one logical backup run."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from cosmos_table_backup import __version__
from cosmos_table_backup.config import BackupConfig
from cosmos_table_backup.discovery import discover_tables, iter_entities
from cosmos_table_backup.encryption import (
    NonceFactory,
    ObjectEncryptor,
    canonical_json,
    generate_dek,
    make_bootstrap,
    wrap_dek_once,
)
from cosmos_table_backup.manifest import BackupManifest, TableManifest
from cosmos_table_backup.serialization import (
    OrderIndependentDigest,
    encode_entity,
    encode_entity_content,
    encode_entity_key,
)
from cosmos_table_backup.storage import BlockBlobWriter
from cosmos_table_backup.telemetry import SafeLogger


class BackupError(RuntimeError):
    """Raised when a run did not produce a completion marker."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _object_aad(backup_id: str, kind: str, index: int) -> bytes:
    return canonical_json(
        {"backup_id": backup_id, "kind": kind, "object_format": 1, "table_index": index}
    )


class BackupRunner:
    def __init__(
        self,
        config: BackupConfig,
        table_service: Any,
        container_client: Any,
        crypto_client: Any,
        logger: SafeLogger,
    ) -> None:
        self._config = config
        self._tables = table_service
        self._container = container_client
        self._crypto = crypto_client
        self._log = logger

    def _writer(self, object_name: str) -> BlockBlobWriter:
        return BlockBlobWriter(
            self._container.get_blob_client(object_name), self._config.block_size
        )

    def run(self) -> str:
        backup_id = str(uuid4())
        prefix = f"backups/{backup_id}"
        started = _now()
        self._log.emit("backup.started", backup_id=backup_id)
        try:
            table_names = discover_tables(self._tables, self._config.excluded_tables)
            dek = generate_dek()
            wrapped_dek = wrap_dek_once(self._crypto, dek)
            nonces = NonceFactory()
            manifest_nonce = nonces.generate()
            table_results: list[TableManifest] = []
            for index, table_name in enumerate(table_names):
                table_started = _now()
                object_name = f"{prefix}/tables/{index:08d}.enc"
                writer = self._writer(object_name)
                encryptor = ObjectEncryptor(
                    dek, nonces.generate(), _object_aad(backup_id, "table", index), writer
                )
                entity_count = 0
                plaintext_hash = hashlib.sha256()
                with (
                    OrderIndependentDigest() as keys_hash,
                    OrderIndependentDigest() as content_hash,
                ):
                    for entity in iter_entities(self._tables, table_name, self._config.page_size):
                        encoded = encode_entity(entity)
                        encryptor.write(encoded)
                        plaintext_hash.update(encoded)
                        keys_hash.update(encode_entity_key(entity))
                        content_hash.update(encode_entity_content(entity))
                        entity_count += 1
                    result = encryptor.finalize()
                    writer.commit()
                    table_results.append(
                        TableManifest(
                            table_name=table_name,
                            object_name=f"tables/{index:08d}.enc",
                            entity_count=entity_count,
                            encrypted_byte_count=result.byte_count,
                            encrypted_sha256=result.sha256,
                            plaintext_sha256=plaintext_hash.hexdigest(),
                            keys_sha256=keys_hash.hexdigest(),
                            entity_content_sha256=content_hash.hexdigest(),
                            started_at=table_started,
                            completed_at=_now(),
                        )
                    )
                self._log.emit(
                    "table_completed",
                    backup_id=backup_id,
                    table_index=index,
                    table_count=len(table_names),
                    entity_count=entity_count,
                    byte_count=result.byte_count,
                    status="succeeded",
                )
            bootstrap = make_bootstrap(backup_id, self._config.key_id, wrapped_dek, manifest_nonce)
            bootstrap_bytes = canonical_json(bootstrap)
            bootstrap_writer = self._writer(f"{prefix}/bootstrap.json")
            bootstrap_writer.write(bootstrap_bytes)
            bootstrap_writer.commit(content_type="application/json")
            manifest = BackupManifest(
                backup_id=backup_id,
                application_version=__version__,
                manifest_version=1,
                consistency="per-table streaming export; no cross-table point-in-time guarantee",
                started_at=started,
                completed_at=_now(),
                tables=tuple(table_results),
            )
            manifest_writer = self._writer(f"{prefix}/manifest.enc")
            manifest_encryptor = ObjectEncryptor(
                dek, manifest_nonce, bootstrap_bytes, manifest_writer
            )
            manifest_encryptor.write(manifest.to_bytes())
            manifest_encryptor.finalize()
            manifest_writer.commit()
            self._log.emit("backup.completed", backup_id=backup_id, status="succeeded")
            return backup_id
        except Exception as exc:
            self._log.emit(
                "backup.failed", backup_id=backup_id, status="failed", error_type=type(exc).__name__
            )
            raise BackupError("backup failed; no valid completion marker was created") from exc
