import base64

import pytest

from cosmos_table_backup.storage import MAX_COMMITTED_BLOCKS, BlockBlobWriter, StorageError


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


@pytest.mark.parametrize("last_block", [b"ab", b"a"])
def test_exact_committed_block_limit(last_block: bytes) -> None:
    assert MAX_COMMITTED_BLOCKS == 50_000
    blob = Blob()
    writer = BlockBlobWriter(blob, 2)
    writer.write(b"ab" * (MAX_COMMITTED_BLOCKS - 1))
    writer.write(last_block)
    assert writer.buffered_bytes <= 2
    writer.commit()

    assert len(blob.staged) == MAX_COMMITTED_BLOCKS
    assert blob.staged[-1][1] == last_block
    assert blob.committed[1]["if_none_match"] == "*"
    expected_ids = [
        base64.b64encode(f"{index:08d}".encode()).decode("ascii")
        for index in range(MAX_COMMITTED_BLOCKS)
    ]
    assert [block_id for block_id, _ in blob.staged] == expected_ids
    assert [block.id for block in blob.committed[0]] == expected_ids
    assert writer.buffered_bytes == 0


@pytest.mark.parametrize("overflow", [b"cd", b"c"])
def test_overflow_is_rejected_before_staging(overflow: bytes) -> None:
    blob = Blob()
    writer = BlockBlobWriter(blob, 2)
    writer.write(b"ab" * MAX_COMMITTED_BLOCKS)
    if len(overflow) == 2:
        with pytest.raises(StorageError, match="50,000 committed-block limit"):
            writer.write(overflow)
    else:
        writer.write(overflow)

    with pytest.raises(StorageError, match="50,000 committed-block limit"):
        writer.commit()
    assert len(blob.staged) == MAX_COMMITTED_BLOCKS
    assert blob.committed is None
    assert writer.buffered_bytes == len(overflow)


def test_commit_failure_is_wrapped() -> None:
    writer = BlockBlobWriter(Blob(fail=True), 4)
    with pytest.raises(StorageError, match="create-only"):
        writer.commit()
