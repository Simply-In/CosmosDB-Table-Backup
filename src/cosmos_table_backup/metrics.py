"""Constant-space numeric stage measurements; no SDK responses or records retained."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

METRIC_FIELDS = frozenset(
    {
        "plaintext_byte_count",
        "entities_per_second",
        "plaintext_bytes_per_second",
        "encrypted_bytes_per_second",
        "page_count",
        "page_fetch_ms",
        "page_fetch_max_ms",
        "stage_block_count",
        "stage_block_bytes",
        "stage_block_ms",
        "stage_block_max_ms",
        "blob_commit_ms",
        "digest_spill_count",
        "digest_spill_bytes",
        "digest_spill_ms",
        "digest_merge_ms",
        "digest_merge_write_bytes",
        "digest_scratch_peak_bytes_bound",
        "local_processing_ms",
    }
)


class StageMetrics:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.values: dict[str, float | int] = {}

    def add(self, field: str, value: float | int) -> None:
        if self.enabled:
            self.values[field] = self.values.get(field, 0) + value

    def maximum(self, field: str, value: float | int) -> None:
        if self.enabled:
            self.values[field] = max(self.values.get(field, 0), value)

    @contextmanager
    def time(self, field: str, maximum: str | None = None) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        started = perf_counter()
        try:
            yield
        finally:
            elapsed = (perf_counter() - started) * 1000
            self.add(field, elapsed)
            if maximum is not None:
                self.maximum(maximum, elapsed)

    def include(self, other: StageMetrics) -> None:
        for field, value in other.values.items():
            if field.endswith(("_max_ms", "_bound")):
                self.maximum(field, value)
            else:
                self.add(field, value)

    def summary(self, duration_ms: float, entities: int, encrypted_bytes: int) -> dict[str, object]:
        seconds = duration_ms / 1000
        fields: dict[str, object] = {field: self.values.get(field, 0) for field in METRIC_FIELDS}
        fields.update(
            duration_ms=duration_ms,
            entity_count=entities,
            byte_count=encrypted_bytes,
            entities_per_second=entities / seconds if seconds > 0 else 0.0,
            plaintext_bytes_per_second=(
                self.values.get("plaintext_byte_count", 0) / seconds if seconds > 0 else 0.0
            ),
            encrypted_bytes_per_second=encrypted_bytes / seconds if seconds > 0 else 0.0,
        )
        return fields
