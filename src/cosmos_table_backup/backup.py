"""Fail-closed orchestration for one logical backup run."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import aclosing
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from cosmos_table_backup import __version__
from cosmos_table_backup.config import BackupConfig
from cosmos_table_backup.discovery import discover_tables_async, iter_entities_async
from cosmos_table_backup.encryption import (
    AsyncObjectEncryptor,
    NonceFactory,
    canonical_json,
    generate_dek,
    make_bootstrap,
    wrap_dek_once_async,
)
from cosmos_table_backup.manifest import BackupManifest, TableManifest
from cosmos_table_backup.metrics import StageMetrics
from cosmos_table_backup.serialization import (
    OrderIndependentDigest,
    encode_entity,
    encode_entity_content,
    encode_entity_key,
)
from cosmos_table_backup.storage import AsyncBlockBlobWriter
from cosmos_table_backup.telemetry import SafeLogger


class BackupError(RuntimeError):
    """Raised when backup completion was not confirmed."""


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

    def _writer(self, object_name: str, metrics: StageMetrics) -> AsyncBlockBlobWriter:
        return AsyncBlockBlobWriter(
            self._container.get_blob_client(object_name),
            self._config.block_size,
            metrics,
            concurrency=self._config.upload_concurrency,
            queue_blocks=self._config.upload_queue_blocks,
        )

    async def run(self) -> str:
        backup_id = str(uuid4())
        prefix = f"backups/{backup_id}"
        started = _now()
        run_clock = perf_counter()
        total_metrics = StageMetrics()
        total_entities = 0
        total_bytes = 0
        self._log.emit("backup.started", backup_id=backup_id)
        try:
            table_names = await discover_tables_async(self._tables, self._config.excluded_tables)
            dek = generate_dek()
            wrapped_dek = await wrap_dek_once_async(self._crypto, dek)
            nonces = NonceFactory()
            manifest_nonce = nonces.generate()
            table_results: list[TableManifest] = []
            for index, table_name in enumerate(table_names):
                table_started = _now()
                table_clock = perf_counter()
                metrics = StageMetrics()
                object_name = f"{prefix}/tables/{index:08d}.enc"
                entity_count = 0
                plaintext_hash = hashlib.sha256()
                async with self._writer(object_name, metrics) as writer:
                    encryptor = AsyncObjectEncryptor(
                        dek, nonces.generate(), _object_aad(backup_id, "table", index), writer
                    )
                    with (
                        OrderIndependentDigest(metrics=metrics) as keys_hash,
                        OrderIndependentDigest(metrics=metrics) as content_hash,
                    ):
                        async with aclosing(
                            iter_entities_async(
                                self._tables, table_name, self._config.page_size, metrics
                            )
                        ) as entities:
                            async for entity in entities:
                                encoded = encode_entity(entity)
                                metrics.add("plaintext_byte_count", len(encoded))
                                await encryptor.write(encoded)
                                plaintext_hash.update(encoded)
                                keys_hash.update(encode_entity_key(entity))
                                content_hash.update(encode_entity_content(entity))
                                entity_count += 1
                        result = await encryptor.finalize()
                        # Finish required local work before committing this table.
                        keys_digest = keys_hash.hexdigest()
                        content_digest = content_hash.hexdigest()
                        await writer.commit()
                        table_results.append(
                            TableManifest(
                                table_name=table_name,
                                object_name=f"tables/{index:08d}.enc",
                                entity_count=entity_count,
                                encrypted_byte_count=result.byte_count,
                                encrypted_sha256=result.sha256,
                                plaintext_sha256=plaintext_hash.hexdigest(),
                                keys_sha256=keys_digest,
                                entity_content_sha256=content_digest,
                                started_at=table_started,
                                completed_at=_now(),
                            )
                        )
                duration_ms = (perf_counter() - table_clock) * 1000
                metrics.add(
                    "local_processing_ms",
                    max(
                        0.0,
                        duration_ms
                        - metrics.values.get("page_fetch_ms", 0)
                        - metrics.values.get("upload_wait_ms", 0)
                        - metrics.values.get("blob_commit_ms", 0),
                    ),
                )
                # Both digest streams coexist; merge output can temporarily duplicate input.
                metrics.maximum(
                    "digest_scratch_peak_bytes_bound",
                    2 * metrics.values.get("digest_spill_bytes", 0),
                )
                total_metrics.include(metrics)
                total_entities += entity_count
                total_bytes += result.byte_count
                self._log.emit(
                    "table_completed",
                    backup_id=backup_id,
                    table_index=index,
                    table_count=len(table_names),
                    status="succeeded",
                    **metrics.summary(duration_ms, entity_count, result.byte_count),
                )
            bootstrap = make_bootstrap(backup_id, self._config.key_id, wrapped_dek, manifest_nonce)
            bootstrap_bytes = canonical_json(bootstrap)
            async with self._writer(f"{prefix}/bootstrap.json", total_metrics) as bootstrap_writer:
                await bootstrap_writer.write(bootstrap_bytes)
                await bootstrap_writer.commit(content_type="application/json")
            manifest = BackupManifest(
                backup_id=backup_id,
                application_version=__version__,
                manifest_version=1,
                consistency="per-table streaming export; no cross-table point-in-time guarantee",
                started_at=started,
                completed_at=_now(),
                tables=tuple(table_results),
            )
            async with self._writer(f"{prefix}/manifest.enc", total_metrics) as manifest_writer:
                manifest_encryptor = AsyncObjectEncryptor(
                    dek, manifest_nonce, bootstrap_bytes, manifest_writer
                )
                await manifest_encryptor.write(manifest.to_bytes())
                await manifest_encryptor.finalize()
                await manifest_writer.commit()
            self._log.emit(
                "backup.completed",
                backup_id=backup_id,
                table_count=len(table_names),
                status="succeeded",
                **total_metrics.summary(
                    (perf_counter() - run_clock) * 1000, total_entities, total_bytes
                ),
            )
            return backup_id
        except (Exception, asyncio.CancelledError) as exc:
            elapsed_seconds = perf_counter() - run_clock
            self._log.emit(
                "backup.failed",
                backup_id=backup_id,
                status="failed",
                error_type=type(exc).__name__,
                duration_ms=elapsed_seconds * 1000,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BackupError("backup failed; completion was not confirmed") from exc
