import base64

import pytest

from cosmos_table_backup.storage import BlockBlobWriter, StorageError


class Blob:
    def __init__(self, fail: bool = False) -> None:
        self.staged: list[tuple[str, bytes]] = []
        self.committed = None
        self.fail = fail

    def stage_block(self, *, block_id: str, data: bytes, length: int) -> None:
        assert len(data) == length
        self.staged.append((block_id, data))

    def commit_block_list(self, blocks, **kwargs):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("failed")
        self.committed = (blocks, kwargs)


def test_streaming_is_bounded_and_commit_is_create_only() -> None:
    blob = Blob()
    writer = BlockBlobWriter(blob, 4)
    writer.write(b"abcdefghij")
    assert writer.buffered_bytes == 2
    assert [data for _, data in blob.staged] == [b"abcd", b"efgh"]
    writer.commit()
    assert [data for _, data in blob.staged] == [b"abcd", b"efgh", b"ij"]
    assert blob.committed[1]["if_none_match"] == "*"
    assert [base64.b64decode(item[0]) for item in blob.staged] == [
        b"00000000",
        b"00000001",
        b"00000002",
    ]
    with pytest.raises(StorageError):
        writer.write(b"late")


def test_commit_failure_is_wrapped() -> None:
    with pytest.raises(StorageError, match="create-only"):
        BlockBlobWriter(Blob(fail=True), 4).commit()
