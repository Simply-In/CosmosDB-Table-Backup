"""Bounded, conditional Blob reads for restore."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from azure.core import MatchConditions


class RestoreStorageError(RuntimeError):
    """Raised when immutable restore input cannot be read safely."""


class AzureRestoreSource:
    def __init__(self, container_client: Any, chunk_size: int) -> None:
        self._container = container_client
        self._chunk_size = chunk_size

    def successful_backup_ids(self) -> set[str]:
        suffix = "/manifest.enc"
        result: set[str] = set()
        for item in self._container.list_blobs(name_starts_with="backups/"):
            name = str(item.name)
            if name.endswith(suffix):
                parts = name.split("/")
                if len(parts) == 3 and parts[0] == "backups":
                    result.add(parts[1])
        return result

    def latest_successful_backup_id(self) -> str:
        suffix = "/manifest.enc"
        candidates: list[tuple[Any, str]] = []
        for item in self._container.list_blobs(name_starts_with="backups/"):
            name = str(item.name)
            parts = name.split("/")
            if name.endswith(suffix) and len(parts) == 3 and parts[0] == "backups":
                candidates.append((item.last_modified, parts[1]))
        if not candidates:
            raise RestoreStorageError("no successfully committed backup is available")
        return max(candidates)[1]

    def snapshot(self, name: str) -> str:
        properties = self._container.get_blob_client(name).get_blob_properties()
        return str(properties.etag)

    def chunks(self, name: str, etag: str) -> Iterator[bytes]:
        blob = self._container.get_blob_client(name)
        downloader = blob.download_blob(
            max_concurrency=1,
            validate_content=True,
            etag=etag,
            match_condition=MatchConditions.IfNotModified,
        )
        for chunk in downloader.chunks():
            view = memoryview(chunk)
            while view:
                yield bytes(view[: self._chunk_size])
                view = view[self._chunk_size :]

    def read_limited(self, name: str, limit: int) -> tuple[bytes, str]:
        etag = self.snapshot(name)
        data = bytearray()
        for chunk in self.chunks(name, etag):
            data.extend(chunk)
            if len(data) > limit:
                raise RestoreStorageError("restore metadata exceeds configured size limit")
        return bytes(data), etag
