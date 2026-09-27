from dataclasses import dataclass

import pytest

from cosmos_table_backup.discovery import DiscoveryError, discover_tables, iter_entities


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
