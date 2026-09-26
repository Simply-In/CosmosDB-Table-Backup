"""Table discovery and bounded entity pagination."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from typing import Any, Protocol

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
    service: TablePager, table_name: str, page_size: int
) -> Iterator[Mapping[str, Any]]:
    """Open the already-approved table and yield one SDK page at a time."""
    table = service.get_table_client(table_name)
    pages = table.query_entities(query_filter="", results_per_page=page_size).by_page()
    for page in pages:
        yield from page
