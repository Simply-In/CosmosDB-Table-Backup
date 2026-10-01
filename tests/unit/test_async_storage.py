import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from cosmos_table_backup.metrics import StageMetrics
from cosmos_table_backup.storage import AsyncBlockBlobWriter, StorageError


class ControlledBlob:
    def __init__(self) -> None:
        self.started: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self.release: dict[str, asyncio.Event] = {}
        self.finished: list[str] = []
        self.active = 0
        self.peak = 0
        self.cancelled = 0
        self.commit_block_list = AsyncMock()

    async def stage_block(self, *, block_id: str, data: bytes, length: int) -> None:
        assert isinstance(data, bytes)
        assert len(data) == length
        event = self.release.setdefault(block_id, asyncio.Event())
        self.active += 1
        self.peak = max(self.peak, self.active)
        await self.started.put((block_id, data))
        try:
            await event.wait()
            self.finished.append(block_id)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


@pytest.mark.parametrize("concurrency,queue_blocks", [(1, 1), (2, 2), (8, 8)])
def test_backpressure_concurrency_and_payload_bounds(concurrency: int, queue_blocks: int) -> None:
    async def scenario() -> None:
        blob = ControlledBlob()
        writer = AsyncBlockBlobWriter(blob, 4, concurrency=concurrency, queue_blocks=queue_blocks)
        produced = asyncio.Queue()

        async def produce() -> None:
            async with writer:
                for index in range(concurrency + queue_blocks + 2):
                    await writer.write(bytes([index]) * 4)
                    produced.put_nowait(index)
                await writer.commit()

        async with asyncio.timeout(5):
            producer = asyncio.create_task(produce())
            initial = [await blob.started.get() for _ in range(concurrency)]
            while len(writer._blocks) < concurrency + queue_blocks + 1:
                await asyncio.sleep(0)
            assert produced.qsize() == concurrency + queue_blocks
            assert writer._queue.qsize() == queue_blocks
            assert writer.buffered_bytes == 0
            assert blob.active == blob.peak == concurrency
            assert not producer.done()
            # Queued bytes remain immutable while a further producer block is blocked.
            assert [payload for _, payload in initial] == [
                bytes([index]) * 4 for index in range(concurrency)
            ]
            for block_id, _ in initial:
                blob.release[block_id].set()
            remaining = concurrency + queue_blocks + 2 - concurrency
            for _ in range(remaining):
                block_id, _ = await blob.started.get()
                blob.release[block_id].set()
            await producer
        assert blob.peak == concurrency
        assert blob.active == 0
        assert writer._queue.empty()
        assert writer._workers == []
        blob.commit_block_list.assert_awaited_once()

    asyncio.run(scenario())


def test_out_of_order_completion_waits_before_create_only_commit() -> None:
    async def scenario() -> None:
        blob = ControlledBlob()
        metrics = StageMetrics()
        writer = AsyncBlockBlobWriter(blob, 4, metrics)
        async with writer:
            await writer.write(b"abcdefghij")
            first, second = await blob.started.get(), await blob.started.get()
            blob.release[second[0]].set()
            await asyncio.sleep(0)
            assert blob.finished == [second[0]]

            async def release() -> None:
                last, payload = await blob.started.get()
                assert payload == b"ij"
                blob.release[last].set()
                await asyncio.sleep(0)
                blob.commit_block_list.assert_not_awaited()
                blob.release[first[0]].set()

            releaser = asyncio.create_task(release())
            await writer.commit()
            await releaser
            with pytest.raises(StorageError):
                await writer.write(b"late")
        blocks = blob.commit_block_list.call_args.args[0]
        assert [base64.b64decode(block.id) for block in blocks] == [
            b"00000000",
            b"00000001",
            b"00000002",
        ]
        assert blob.commit_block_list.call_args.kwargs["if_none_match"] == "*"
        assert metrics.values["stage_block_count"] == 3
        assert metrics.values["stage_block_bytes"] == 10
        assert metrics.values["upload_wait_ms"] >= 0
        with pytest.raises(StorageError):
            await writer.commit()
        with pytest.raises(StorageError):
            await writer.__aenter__()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_at", ["queued", "last"])
def test_stage_failure_cancels_siblings_and_prevents_commit(failure_at: str) -> None:
    async def scenario() -> None:
        blob = ControlledBlob()
        original = blob.stage_block

        async def failing_stage(**kwargs):  # type: ignore[no-untyped-def]
            if base64.b64decode(kwargs["block_id"]) == b"00000001":
                raise RuntimeError("stage failed")
            await original(**kwargs)

        blob.stage_block = failing_stage
        writer = AsyncBlockBlobWriter(blob, 4, queue_blocks=1)
        before = asyncio.all_tasks()
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            async with asyncio.timeout(5), writer:
                await writer.write(b"x" * (40 if failure_at == "queued" else 8))
                await writer.commit()
        blob.commit_block_list.assert_not_awaited()
        assert blob.active == 0
        assert blob.cancelled == 1
        assert writer._queue.empty()
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("during", ["enqueue", "drain", "producer"])
def test_cancellation_and_producer_failure_close_all_workers(during: str) -> None:
    async def scenario() -> None:
        blob = ControlledBlob()
        writer = AsyncBlockBlobWriter(blob, 4, concurrency=1, queue_blocks=1)
        before = asyncio.all_tasks()

        async def produce() -> None:
            async with writer:
                await writer.write(b"abcdefgh" if during != "enqueue" else b"x" * 40)
                if during == "producer":
                    raise ValueError("serialization failed")
                await writer.commit()

        producer = asyncio.create_task(produce())
        async with asyncio.timeout(5):
            await blob.started.get()
            if during == "producer":
                with pytest.raises(ExceptionGroup) as error:
                    await producer
                assert isinstance(error.value.exceptions[0], ValueError)
            else:
                producer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await producer
        assert blob.active == 0
        assert blob.cancelled == 1
        assert writer._queue.empty()
        assert writer.buffered_bytes == 0
        blob.commit_block_list.assert_not_awaited()
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("last", [b"ab", b"a"])
@pytest.mark.parametrize("overflow", [False, True])
def test_async_block_limit_reserved_before_enqueue(
    monkeypatch: pytest.MonkeyPatch, last: bytes, overflow: bool
) -> None:
    monkeypatch.setattr("cosmos_table_backup.storage.MAX_COMMITTED_BLOCKS", 3)

    async def scenario() -> None:
        blob = AsyncMock()
        writer = AsyncBlockBlobWriter(blob, 2)

        async def produce() -> None:
            async with writer:
                await writer.write(b"abab" + last)
                if overflow:
                    await writer.write(b"ab" if len(last) == 2 else b"bc")
                await writer.commit()

        if overflow:
            with pytest.raises(ExceptionGroup) as error:
                await produce()
            assert isinstance(error.value.exceptions[0], StorageError)
            blob.commit_block_list.assert_not_awaited()
            assert blob.stage_block.await_count <= 3
        else:
            await produce()
            assert blob.stage_block.await_count == 3
            blocks = blob.commit_block_list.call_args.args[0]
            assert len(blocks) == 3
            assert blob.stage_block.call_args.kwargs["data"] == last
        assert writer._queue.empty()

    asyncio.run(scenario())


def test_empty_commit_failure_and_writer_validation() -> None:
    async def scenario() -> None:
        blob = AsyncMock()
        blob.commit_block_list.side_effect = RuntimeError("conflict")
        writer = AsyncBlockBlobWriter(blob, 4)
        with pytest.raises(StorageError):
            await writer.write(b"x")
        with pytest.raises(ExceptionGroup) as error:
            async with writer:
                with pytest.raises(StorageError):
                    await writer.__aenter__()
                await writer.commit()
        assert isinstance(error.value.exceptions[0], StorageError)
        blob.stage_block.assert_not_awaited()
        assert blob.commit_block_list.call_args.args[0] == []

    asyncio.run(scenario())
    for kwargs in ({"block_size": 0}, {"concurrency": 0}, {"queue_blocks": 9}):
        with pytest.raises(StorageError):
            AsyncBlockBlobWriter(AsyncMock(), **({"block_size": 4} | kwargs))
