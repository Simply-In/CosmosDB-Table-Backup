from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from cosmos_table_backup.restore_storage import AzureRestoreSource, RestoreStorageError


@dataclass
class Item:
    name: str
    last_modified: datetime = datetime(2026, 1, 1, tzinfo=UTC)


class Download:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def chunks(self):  # type: ignore[no-untyped-def]
        yield self.data


class Blob:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.kwargs = None

    def get_blob_properties(self):  # type: ignore[no-untyped-def]
        return type("Properties", (), {"etag": '"etag"'})()

    def download_blob(self, **kwargs):  # type: ignore[no-untyped-def]
        self.kwargs = kwargs
        return Download(self.data)


class Container:
    def __init__(self) -> None:
        self.blob = Blob(b"abcdefghij")

    def list_blobs(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs == {"name_starts_with": "backups/"}
        return [
            Item("backups/id/manifest.enc"),
            Item("backups/newer/manifest.enc", datetime(2026, 2, 1, tzinfo=UTC)),
            Item("backups/partial/bootstrap.json"),
            Item("other/id/manifest.enc"),
            Item("backups/too/deep/manifest.enc"),
        ]

    def get_blob_client(self, name: str) -> Blob:
        return self.blob


def test_discovers_only_manifest_marked_runs_and_bounds_chunks() -> None:
    container = Container()
    source = AzureRestoreSource(container, 4)
    assert source.successful_backup_ids() == {"id", "newer"}
    assert source.latest_successful_backup_id() == "newer"
    assert list(source.chunks("name", '"etag"')) == [b"abcd", b"efgh", b"ij"]
    assert container.blob.kwargs["max_concurrency"] == 1
    assert container.blob.kwargs["validate_content"] is True


def test_limited_metadata_read() -> None:
    source = AzureRestoreSource(Container(), 4)
    assert source.read_limited("name", 10)[0] == b"abcdefghij"
    with pytest.raises(RestoreStorageError):
        source.read_limited("name", 9)
