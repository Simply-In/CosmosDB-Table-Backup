"""Table discovery and bounded entity pagination."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Iterable, Iterator, Mapping
from contextlib import AsyncExitStack, aclosing
from types import TracebackType
from typing import Any, Protocol

from cosmos_table_backup.metrics import StageMetrics

EXCLUDED_TABLES = frozenset({"cards"})


class DiscoveryError(RuntimeError):
    """Raised when no usable table set can be discovered."""


class TablePager(Protocol):
    def list_tables(self) -> Iterable[Any]: ...
    def get_table_client(self, table_name: str) -> Any: ...


def discover_tables(
    service: TablePager, excluded_tables: Collection[str] = EXCLUDED_TABLES
) -> list[str]:
    """Enumerate all tables, excluding exact protected names before table clients are opened."""
    exclusions = frozenset(excluded_tables) | EXCLUDED_TABLES
    try:
        names = sorted(
            str(item.name if hasattr(item, "name") else item["name"])
            for item in service.list_tables()
        )
    except Exception as exc:
        raise DiscoveryError("table discovery failed") from exc
    included = [name for name in names if name not in exclusions]
    if not included:
        raise DiscoveryError("no included tables were discovered")
    return included


def iter_entities(
    service: TablePager, table_name: str, page_size: int, metrics: StageMetrics | None = None
) -> Iterator[Mapping[str, Any]]:
    """Open the already-approved table and yield one SDK page at a time."""
    table = service.get_table_client(table_name)
    pages = table.query_entities(query_filter="", results_per_page=page_size).by_page()
    if metrics is None:
        for page in pages:
            yield from page
        return
    pages = iter(pages)
    while True:
        try:
            with metrics.time("page_fetch_ms", "page_fetch_max_ms"):
                page = next(pages)
        except StopIteration:
            return
        metrics.add("page_count", 1)
        yield from page


async def discover_tables_async(
    service: Any, excluded_tables: Collection[str] = EXCLUDED_TABLES
) -> list[str]:
    exclusions = frozenset(excluded_tables) | EXCLUDED_TABLES
    try:
        names = [
            str(item.name if hasattr(item, "name") else item["name"])
            async for item in service.list_tables()
        ]
    except Exception as exc:
        raise DiscoveryError("table discovery failed") from exc
    included = sorted(name for name in names if name not in exclusions)
    if not included:
        raise DiscoveryError("no included tables were discovered")
    return included


async def iter_entities_async(
    service: Any, table_name: str, page_size: int, metrics: StageMetrics
) -> AsyncGenerator[Mapping[str, Any]]:
    """Advance exactly one async SDK page at a time, without prefetch."""
    async with aclosing(_iter_pages_async(service, table_name, page_size, metrics)) as pages:
        async for page in pages:
            async for entity in page:
                yield entity


async def _iter_pages_async(
    service: Any, table_name: str, page_size: int, metrics: StageMetrics
) -> AsyncGenerator[AsyncIterator[Mapping[str, Any]]]:
    async with service.get_table_client(table_name) as table:
        pages = table.query_entities(query_filter="", results_per_page=page_size).by_page()
        try:
            while True:
                try:
                    with metrics.time("page_fetch_ms", "page_fetch_max_ms"):
                        page = await anext(pages)
                except StopAsyncIteration:
                    return
                metrics.add("page_count", 1)
                yield page
                del page
        finally:
            close = getattr(pages, "aclose", None)
            if close is not None:
                await close()


class AsyncEntitySource:
    """Own one producer and reserve one queued-page slot before each SDK fetch."""

    def __init__(
        self, service: Any, table_name: str, page_size: int, metrics: StageMetrics
    ) -> None:
        self._service = service
        self._table_name = table_name
        self._page_size = page_size
        self._metrics = metrics
        self._queue: asyncio.Queue[AsyncIterator[Mapping[str, Any]] | None] = asyncio.Queue(1)
        self._slot = asyncio.Semaphore(1)
        self._page: AsyncIterator[Mapping[str, Any]] | None = None
        self._group: asyncio.TaskGroup | None = None
        self._producer: asyncio.Task[None] | None = None
        self._owner: asyncio.Task[Any] | None = None
        self._closed = False
        self._exhausted = False

    async def __aenter__(self) -> AsyncEntitySource:
        if self._closed or self._group is not None:
            raise DiscoveryError("entity source is closed or already started")
        self._group = asyncio.TaskGroup()
        await self._group.__aenter__()
        self._owner = asyncio.current_task()
        self._producer = self._group.create_task(self._produce())
        self._producer.add_done_callback(self._propagate_cancellation)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True
        if self._producer is not None:
            self._producer.cancel()
        try:
            if self._group is not None:
                await self._group.__aexit__(exc_type, exc, traceback)
        finally:
            self._producer = None
            self._owner = None
            async with AsyncExitStack() as pages:
                if self._page is not None:
                    close = getattr(self._page, "aclose", None)
                    if close is not None:
                        pages.push_async_callback(close)
                    self._page = None
                while not self._queue.empty():
                    page = self._queue.get_nowait()
                    close = getattr(page, "aclose", None)
                    if close is not None:
                        pages.push_async_callback(close)

    def _propagate_cancellation(self, producer: asyncio.Task[None]) -> None:
        # TaskGroup does not interrupt its parent for a child's CancelledError.
        if producer.cancelled() and not self._closed and self._owner is not None:
            self._owner.cancel()

    async def _produce(self) -> None:
        async with aclosing(
            _iter_pages_async(self._service, self._table_name, self._page_size, self._metrics)
        ) as pages:
            while True:
                with self._metrics.time("source_backpressure_ms"):
                    await self._slot.acquire()
                try:
                    page = await anext(pages)
                except StopAsyncIteration:
                    break
                self._queue.put_nowait(page)
                del page
        # Closing the table client is required work, before reporting end-of-stream.
        self._queue.put_nowait(None)

    def __aiter__(self) -> AsyncEntitySource:
        return self

    async def __anext__(self) -> Mapping[str, Any]:
        if self._closed or self._producer is None:
            raise DiscoveryError("entity source is not open")
        if self._exhausted:
            raise StopAsyncIteration
        while True:
            if self._page is None:
                with self._metrics.time("source_wait_ms"):
                    self._page = await self._queue.get()
                self._slot.release()
                if self._page is None:
                    self._exhausted = True
                    raise StopAsyncIteration
                # Start the reserved next fetch before processing this materialized page.
                await asyncio.sleep(0)
            try:
                return await anext(self._page)
            except StopAsyncIteration:
                self._page = None
