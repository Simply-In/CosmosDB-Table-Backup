"""Bounded block-blob streaming with create-only commit semantics."""

from __future__ import annotations

import base64
from typing import Any

from azure.storage.blob import BlobBlock

MAX_COMMITTED_BLOCKS = 50_000


class StorageError(RuntimeError):
    """Raised when a blob cannot be safely committed."""


class BlockBlobWriter:
    """Buffer at most one configured block and atomically commit a new blob."""

    def __init__(self, blob_client: Any, block_size: int) -> None:
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
        self._client.stage_block(block_id=block_id, data=payload, length=len(payload))
        self._blocks.append(BlobBlock(block_id=block_id))
        self._index += 1
        self._buffer.clear()

    def commit(self, *, content_type: str = "application/octet-stream") -> None:
        if self._closed:
            raise StorageError("writer is closed")
        self._stage()
        try:
            from azure.storage.blob import ContentSettings

            self._client.commit_block_list(
                self._blocks,
                content_settings=ContentSettings(content_type=content_type),
                if_none_match="*",
            )
        except Exception as exc:
            raise StorageError("create-only blob commit failed") from exc
        self._closed = True
