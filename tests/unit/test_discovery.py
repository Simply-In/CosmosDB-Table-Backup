import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.async_paging import AsyncItemPaged

from cosmos_table_backup.discovery import (
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
