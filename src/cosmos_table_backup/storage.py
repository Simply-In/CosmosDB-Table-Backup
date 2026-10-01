"""Bounded block-blob streaming with create-only commit semantics."""

from __future__ import annotations

import asyncio
import base64
from types import TracebackType
from typing import Any

from azure.storage.blob import BlobBlock

from cosmos_table_backup.metrics import StageMetrics

MAX_COMMITTED_BLOCKS = 50_000


class StorageError(RuntimeError):
    """Raised when a blob cannot be safely committed."""


class BlockBlobWriter:
    """Buffer at most one configured block and atomically commit a new blob."""

    def __init__(
        self, blob_client: Any, block_size: int, metrics: StageMetrics | None = None
    ) -> None:
        self._metrics = metrics or StageMetrics(enabled=False)
        self._client = blob_client
        self._block_size = block_size
        self._buffer = bytearray()
        self._blocks: list[BlobBlock] = []
        self._index = 0
        self._closed = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def write(self, data: bytes) -> None:
        if self._closed:
            raise StorageError("writer is closed")
        view = memoryview(data)
        while view:
            take = min(self._block_size - len(self._buffer), len(view))
            self._buffer.extend(view[:take])
            view = view[take:]
            if len(self._buffer) == self._block_size:
                self._stage()

    def _stage(self) -> None:
        if not self._buffer:
            return
        if len(self._blocks) >= MAX_COMMITTED_BLOCKS:
            raise StorageError("blob exceeds the 50,000 committed-block limit")
        block_id = base64.b64encode(f"{self._index:08d}".encode()).decode("ascii")
        payload = bytes(self._buffer)
        with self._metrics.time("stage_block_ms", "stage_block_max_ms"):
            self._client.stage_block(block_id=block_id, data=payload, length=len(payload))
        self._metrics.add("stage_block_count", 1)
        self._metrics.add("stage_block_bytes", len(payload))
        self._blocks.append(BlobBlock(block_id=block_id))
        self._index += 1
        self._buffer.clear()

    def commit(self, *, content_type: str = "application/octet-stream") -> None:
        if self._closed:
            raise StorageError("writer is closed")
        self._stage()
        try:
            from azure.storage.blob import ContentSettings

            with self._metrics.time("blob_commit_ms"):
                self._client.commit_block_list(
                    self._blocks,
                    content_settings=ContentSettings(content_type=content_type),
                    if_none_match="*",
                )
        except Exception as exc:
            raise StorageError("create-only blob commit failed") from exc
        self._closed = True


class AsyncBlockBlobWriter:
    """Own a fixed worker set and bounded immutable payload queue per object."""

    def __init__(
        self,
        blob_client: Any,
        block_size: int,
        metrics: StageMetrics | None = None,
        *,
        concurrency: int = 2,
        queue_blocks: int = 2,
    ) -> None:
        if block_size < 1 or not 1 <= concurrency <= 8 or not 1 <= queue_blocks <= 8:
            raise StorageError("invalid block size or upload limits")
        self._client = blob_client
        self._block_size = block_size
        self._metrics = metrics or StageMetrics(enabled=False)
        self._concurrency = concurrency
        self._queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(queue_blocks)
        self._buffer = bytearray()
        self._blocks: list[BlobBlock] = []
        self._group: asyncio.TaskGroup | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._closed = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    async def __aenter__(self) -> AsyncBlockBlobWriter:
        if self._closed or self._group is not None:
            raise StorageError("writer is closed or already started")
        self._group = asyncio.TaskGroup()
        await self._group.__aenter__()
        for _ in range(self._concurrency):
            self._workers.append(self._group.create_task(self._upload()))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True
        for worker in self._workers:
            worker.cancel()
        try:
            if self._group is not None:
                await self._group.__aexit__(exc_type, exc, traceback)
        finally:
            self._buffer.clear()
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            self._workers.clear()

    def _check_open(self) -> None:
        if self._closed or self._group is None:
            raise StorageError("writer is closed or not started")
        for worker in self._workers:
            if worker.done():
                worker.result()

    async def _upload(self) -> None:
        while True:
            block_id, payload = await self._queue.get()
            try:
                with self._metrics.time("stage_block_ms", "stage_block_max_ms"):
                    await self._client.stage_block(
                        block_id=block_id, data=payload, length=len(payload)
                    )
                self._metrics.add("stage_block_count", 1)
                self._metrics.add("stage_block_bytes", len(payload))
            finally:
                self._queue.task_done()
                # Do not retain the previous payload while waiting for another block.
                del payload

    async def write(self, data: bytes) -> None:
        self._check_open()
        view = memoryview(data)
        while view:
            take = min(self._block_size - len(self._buffer), len(view))
            self._buffer.extend(view[:take])
            view = view[take:]
            if len(self._buffer) == self._block_size:
                await self._stage()
        # Queue.put need not suspend when capacity is available.
        await asyncio.sleep(0)

    async def _stage(self) -> None:
        if not self._buffer:
            return
        if len(self._blocks) >= MAX_COMMITTED_BLOCKS:
            raise StorageError("blob exceeds the 50,000 committed-block limit")
        block_id = base64.b64encode(f"{len(self._blocks):08d}".encode()).decode("ascii")
        payload = bytes(self._buffer)
        self._buffer.clear()
        self._blocks.append(BlobBlock(block_id=block_id))
        with self._metrics.time("upload_wait_ms"):
            await self._queue.put((block_id, payload))

    async def commit(self, *, content_type: str = "application/octet-stream") -> None:
        self._check_open()
        await self._stage()
        with self._metrics.time("upload_wait_ms"):
            await self._queue.join()
        self._check_open()
        try:
            from azure.storage.blob import ContentSettings

            with self._metrics.time("blob_commit_ms"):
                await self._client.commit_block_list(
                    self._blocks,
                    content_settings=ContentSettings(content_type=content_type),
                    if_none_match="*",
                )
        except Exception as exc:
            raise StorageError("create-only blob commit failed") from exc
        self._closed = True
