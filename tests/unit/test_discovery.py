import asyncio
import weakref
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.async_paging import AsyncItemPaged

from cosmos_table_backup.discovery import (
    AsyncEntitySource,
    DiscoveryError,
    discover_tables,
    discover_tables_async,
    iter_entities,
    iter_entities_async,
)
from cosmos_table_backup.metrics import StageMetrics


@dataclass
class Item:
    name: str


class Service:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.opened: list[str] = []

    def list_tables(self):  # type: ignore[no-untyped-def]
        return [Item(name) for name in self.names]

    def get_table_client(self, name: str):  # type: ignore[no-untyped-def]
        self.opened.append(name)
        return Table()


class Table:
    def query_entities(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs == {"query_filter": "", "results_per_page": 2}
        return self

    def by_page(self):  # type: ignore[no-untyped-def]
        yield [{"x": 1}, {"x": 2}]
        yield [{"x": 3}]


def test_cards_and_configured_names_are_excluded_before_any_read() -> None:
    service = Service(["z", "cards", "Cards", "a"])
    assert discover_tables(service, {"z"}) == ["Cards", "a"]
    assert service.opened == []


@pytest.mark.parametrize("names", [[], ["cards"]])
def test_missing_or_excluded_only_discovery_fails(names: list[str]) -> None:
    service = Service(names)
    with pytest.raises(DiscoveryError):
        discover_tables(service)


def test_discovery_exception_is_safe() -> None:
    service = Service([])
    service.list_tables = lambda: (_ for _ in ()).throw(RuntimeError("sensitive"))  # type: ignore[method-assign]
    with pytest.raises(DiscoveryError, match="table discovery failed"):
        discover_tables(service)


def test_entity_iteration_uses_sdk_pages() -> None:
    service = Service(["a"])
    assert list(iter_entities(service, "a", 2)) == [{"x": 1}, {"x": 2}, {"x": 3}]
    assert service.opened == ["a"]


def test_async_sdk_paging_is_sequential_and_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        now = [0.0]
        monkeypatch.setattr("cosmos_table_backup.metrics.perf_counter", lambda: now[0])
        fetches = []

        async def fetch(token):  # type: ignore[no-untyped-def]
            fetches.append(token)
            now[0] += 0.25
            return token

        async def extract(token):  # type: ignore[no-untyped-def]
            return ("last" if token is None else None, [{"x": 1}, {"x": 2}])

        table = AsyncMock()
        table.__aenter__.return_value = table
        table.query_entities = Mock(return_value=AsyncItemPaged(fetch, extract))
        service = Mock()
        service.get_table_client.return_value = table
        metrics = StageMetrics()
        values = []
        async for entity in iter_entities_async(service, "approved", 2, metrics):
            assert len(fetches) == 1 if len(values) < 2 else len(fetches) == 2
            now[0] += 10
            values.append(entity)
        assert values == [{"x": 1}, {"x": 2}] * 2
        assert fetches == [None, "last"]
        assert metrics.values["page_count"] == 2
        assert metrics.values["page_fetch_ms"] == 500
        table.query_entities.assert_called_once_with(query_filter="", results_per_page=2)
        table.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


def async_service(pager):  # type: ignore[no-untyped-def]
    table = AsyncMock()
    table.__aenter__.return_value = table
    table.query_entities = Mock(return_value=pager)
    service = Mock()
    service.get_table_client.return_value = table
    return service, table


def test_source_reserves_capacity_before_fetch_and_overlaps_consumption() -> None:
    async def scenario() -> None:
        fetches = []
        fetched_second = asyncio.Event()

        async def fetch(token):  # type: ignore[no-untyped-def]
            fetches.append(token)
            if token == "1":
                fetched_second.set()
            return int(token or "0")

        async def extract(index):  # type: ignore[no-untyped-def]
            return (
                str(index + 1) if index < 2 else None,
                [{"index": index, "entity": number} for number in range(2)],
            )

        service, table = async_service(AsyncItemPaged(fetch, extract))
        metrics = StageMetrics()
        before = asyncio.all_tasks()
        source = AsyncEntitySource(service, "approved", 2, metrics)
        async with asyncio.timeout(5), source:
            assert await anext(source) == {"index": 0, "entity": 0}
            await fetched_second.wait()
            for _ in range(5):
                await asyncio.sleep(0)
            assert fetches == [None, "1"]
            assert source._queue.qsize() == 1
            assert len(asyncio.all_tasks() - before) == 1
            assert await anext(source) == {"index": 0, "entity": 1}
            assert fetches == [None, "1"]
            assert await anext(source) == {"index": 1, "entity": 0}
            assert fetches == [None, "1", "2"]
            remaining = [entity async for entity in source]
            assert remaining == [
                {"index": 1, "entity": 1},
                {"index": 2, "entity": 0},
                {"index": 2, "entity": 1},
            ]
            with pytest.raises(StopAsyncIteration):
                await anext(source)
        assert asyncio.all_tasks() == before
        assert source._queue.empty()
        assert source._page is None
        assert metrics.values["page_count"] == 3
        assert metrics.values["source_wait_ms"] >= 0
        assert metrics.values["source_backpressure_ms"] >= 0
        table.query_entities.assert_called_once_with(query_filter="", results_per_page=2)
        table.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("sizes", [[0], [0, 2, 0, 1, 0], [2, 2, 1], []])
def test_source_keeps_empty_partial_pages_order_and_entity_identity(sizes: list[int]) -> None:
    async def scenario() -> None:
        pages = [
            [{"page": index, "item": item} for item in range(size)]
            for index, size in enumerate(sizes)
        ]

        async def fetch(token):  # type: ignore[no-untyped-def]
            index = int(token or 0)
            if index == len(pages):
                raise StopAsyncIteration
            return index

        async def extract(index):  # type: ignore[no-untyped-def]
            return str(index + 1), pages[index]

        service, table = async_service(AsyncItemPaged(fetch, extract))
        metrics = StageMetrics()
        async with AsyncEntitySource(service, "approved", 2, metrics) as source:
            actual = [entity async for entity in source]
        expected = [entity for page in pages for entity in page]
        assert len(actual) == len(expected)
        assert all(left is right for left, right in zip(actual, expected, strict=True))
        assert metrics.values.get("page_count", 0) == len(sizes)
        table.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


def test_source_releases_pages_with_a_bound_independent_of_table_size() -> None:
    class Entity(dict):
        pass

    async def scenario() -> None:
        references = []

        async def fetch(token):  # type: ignore[no-untyped-def]
            return int(token or 0)

        async def extract(index):  # type: ignore[no-untyped-def]
            entities = [Entity(index=index, item=item) for item in range(2)]
            references.extend(weakref.ref(entity) for entity in entities)
            return str(index + 1) if index < 99 else None, entities

        service, _ = async_service(AsyncItemPaged(fetch, extract))
        source = AsyncEntitySource(service, "approved", 2, StageMetrics())
        seen = 0
        async with source:
            async for entity in source:
                assert entity["index"] == (seen // 2)
                seen += 1
                await asyncio.sleep(0)
                assert sum(reference() is not None for reference in references) <= 4
                assert source._queue.qsize() <= 1
        del entity
        assert seen == 200
        assert all(reference() is None for reference in references)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["first_fetch", "next_fetch", "page", "close"])
def test_source_failure_interrupts_waiting_consumer_and_closes_client(phase: str) -> None:
    async def scenario() -> None:
        before = asyncio.all_tasks()

        async def fetch(token):  # type: ignore[no-untyped-def]
            if phase == "first_fetch" or (phase == "next_fetch" and token is not None):
                raise RuntimeError("sensitive source error")
            return token

        async def page():  # type: ignore[no-untyped-def]
            if phase == "page":
                raise RuntimeError("sensitive page error")
            yield {"x": 1}

        async def extract(token):  # type: ignore[no-untyped-def]
            return "last" if token is None else None, page()

        service, table = async_service(AsyncItemPaged(fetch, extract))
        if phase == "close":
            table.__aexit__.side_effect = RuntimeError("sensitive close error")
        source = AsyncEntitySource(service, "approved", 2, StageMetrics())
        async with asyncio.timeout(5):
            with pytest.raises(ExceptionGroup) as raised:
                async with source:
                    if phase == "next_fetch":
                        await anext(source)
                        await asyncio.Event().wait()
                    else:
                        async for _ in source:
                            pass
        assert raised.value.subgroup(RuntimeError) is not None
        assert asyncio.all_tasks() == before
        assert source._queue.empty()
        assert source._page is None
        table.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["fetch", "full_queue", "consumer"])
def test_source_cancellation_or_early_exit_cleans_all_tasks(phase: str) -> None:
    async def scenario() -> None:
        before = asyncio.all_tasks()
        ready = asyncio.Event()
        fetch_closed = asyncio.Event()

        async def fetch(token):  # type: ignore[no-untyped-def]
            if phase == "fetch":
                ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    fetch_closed.set()
            return token

        async def extract(token):  # type: ignore[no-untyped-def]
            return "last" if token is None else None, [{"x": 1}, {"x": 2}]

        service, table = async_service(AsyncItemPaged(fetch, extract))
        source = AsyncEntitySource(service, "approved", 2, StageMetrics())

        async def consume() -> None:
            async with source:
                if phase == "fetch":
                    await anext(source)
                elif phase == "full_queue":
                    await anext(source)
                    await asyncio.sleep(0)
                    assert source._queue.full()
                    ready.set()
                    await asyncio.Event().wait()
                else:
                    await anext(source)

        task = asyncio.create_task(consume())
        async with asyncio.timeout(5):
            if phase != "consumer":
                await ready.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
        assert phase != "fetch" or fetch_closed.is_set()
        table.__aexit__.assert_awaited_once()
        assert source._queue.empty()
        assert source._page is None
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_source_rejects_invalid_lifecycle() -> None:
    async def scenario() -> None:
        source = AsyncEntitySource(Mock(), "approved", 2, StageMetrics())
        unopened_read = anext(source)
        with pytest.raises(DiscoveryError, match="not open"):
            await unopened_read
        source._closed = True
        closed_entry = source.__aenter__()
        with pytest.raises(DiscoveryError, match="closed"):
            await closed_entry

    asyncio.run(scenario())


def test_source_sdk_cancellation_propagates_without_a_blocked_consumer() -> None:
    async def scenario() -> None:
        before = asyncio.all_tasks()

        async def fetch(token):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError

        service, table = async_service(AsyncItemPaged(fetch, AsyncMock()))

        async def consume() -> None:
            async with AsyncEntitySource(service, "approved", 2, StageMetrics()) as source:
                await anext(source)

        task = asyncio.create_task(consume())
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.shield(task)
        assert task.cancelled()
        assert asyncio.all_tasks() == before
        table.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("names", [["z", "cards", "Cards", "a"], [], ["cards"]])
def test_async_discovery_exclusions_and_failure(names: list[str]) -> None:
    async def scenario() -> None:
        async def listed():  # type: ignore[no-untyped-def]
            for name in names:
                yield Item(name)

        service = Mock()
        service.list_tables = listed
        if names and names != ["cards"]:
            assert await discover_tables_async(service, {"z"}) == ["Cards", "a"]
        else:
            with pytest.raises(DiscoveryError):
                await discover_tables_async(service)
        service.get_table_client.assert_not_called()
        service.list_tables = Mock(side_effect=RuntimeError("secret"))
        with pytest.raises(DiscoveryError, match="table discovery failed"):
            await discover_tables_async(service)

    asyncio.run(scenario())
