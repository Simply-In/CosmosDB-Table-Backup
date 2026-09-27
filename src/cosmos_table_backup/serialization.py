"""Lossless, deterministic JSON Lines encoding for Table entities."""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
import math
import shutil
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from azure.data.tables import EdmType, EntityProperty

_TYPE_TAGS = {
    EdmType.BINARY: "Binary",
    EdmType.BOOLEAN: "Boolean",
    EdmType.DATETIME: "DateTime",
    EdmType.DOUBLE: "Double",
    EdmType.GUID: "Guid",
    EdmType.INT32: "Int32",
    EdmType.INT64: "Int64",
    EdmType.STRING: "String",
}
_TAG_TYPES = {tag: edm for edm, tag in _TYPE_TAGS.items()}


class SerializationError(ValueError):
    """Raised for an unsupported or ambiguous property value."""


class OrderIndependentDigest:
    """Hash a multiset of records using a bounded-memory external digest sort."""

    _DIGEST_BYTES = 32
    _MERGE_FAN_IN = 32

    def __init__(
        self,
        max_digests_in_memory: int = 32_768,
        scratch_directory: Path | None = None,
    ) -> None:
        if max_digests_in_memory < 1:
            raise ValueError("digest memory bound must be positive")
        self._max_in_memory = max_digests_in_memory
        self._scratch_root = Path.cwd() if scratch_directory is None else scratch_directory
        self._work_directory: Path | None = None
        self._chunk_paths: list[Path] = []
        self._digests: list[bytes] = []
        self._next_chunk = 0
        self._closed = False
        self._ever_spilled = False

    @property
    def in_memory_count(self) -> int:
        return len(self._digests)

    @property
    def spilled(self) -> bool:
        return self._ever_spilled

    @property
    def scratch_path(self) -> Path | None:
        return self._work_directory

    def __enter__(self) -> OrderIndependentDigest:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def update(self, canonical_record: bytes) -> None:
        if self._closed:
            raise ValueError("digest is closed")
        self._digests.append(hashlib.sha256(canonical_record).digest())
        if len(self._digests) >= self._max_in_memory:
            self._spill_chunk()

    def _new_chunk_path(self) -> Path:
        if self._work_directory is None:
            self._work_directory = self._scratch_root / f".ctb-digests-{uuid4()}"
            self._work_directory.mkdir(mode=0o700)
        path = self._work_directory / f"{self._next_chunk:08d}.bin"
        self._next_chunk += 1
        return path

    def _spill_chunk(self) -> None:
        if not self._digests:
            return
        path = self._new_chunk_path()
        try:
            with path.open("xb") as stream:
                for digest in sorted(self._digests):
                    stream.write(digest)
        except Exception:
            self.close()
            raise
        self._chunk_paths.append(path)
        self._digests.clear()
        self._ever_spilled = True

    @classmethod
    def _read_chunk(cls, path: Path) -> Iterator[bytes]:
        with path.open("rb") as stream:
            while digest := stream.read(cls._DIGEST_BYTES):
                if len(digest) != cls._DIGEST_BYTES:
                    raise ValueError("corrupt digest sort chunk")
                yield digest

    def _merge_chunk_group(self, paths: list[Path]) -> Path:
        merged_path = self._new_chunk_path()
        with merged_path.open("xb") as output:
            for digest in heapq.merge(*(self._read_chunk(path) for path in paths)):
                output.write(digest)
        for path in paths:
            path.unlink()
        return merged_path

    def _consolidate_chunks(self) -> None:
        while len(self._chunk_paths) > self._MERGE_FAN_IN:
            merged: list[Path] = []
            for offset in range(0, len(self._chunk_paths), self._MERGE_FAN_IN):
                group = self._chunk_paths[offset : offset + self._MERGE_FAN_IN]
                merged.append(group[0] if len(group) == 1 else self._merge_chunk_group(group))
            self._chunk_paths = merged

    def _update_from_chunks(self, aggregate: Any) -> None:
        self._spill_chunk()
        self._consolidate_chunks()
        for digest in heapq.merge(*(self._read_chunk(path) for path in self._chunk_paths)):
            aggregate.update(digest)

    def hexdigest(self) -> str:
        if self._closed:
            raise ValueError("digest is closed")
        aggregate = hashlib.sha256()
        try:
            if self._chunk_paths:
                self._update_from_chunks(aggregate)
            else:
                for digest in sorted(self._digests):
                    aggregate.update(digest)
            return aggregate.hexdigest()
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._digests.clear()
        if self._work_directory is not None and self._work_directory.exists():
            shutil.rmtree(self._work_directory)
        self._chunk_paths.clear()
        self._work_directory = None
        self._closed = True


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SerializationError("DateTime values must include a UTC offset")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _infer(value: Any) -> tuple[EdmType, Any]:
    if isinstance(value, bool):
        return EdmType.BOOLEAN, value
    if isinstance(value, str):
        return EdmType.STRING, value
    if isinstance(value, bytes):
        return EdmType.BINARY, value
    if isinstance(value, datetime):
        return EdmType.DATETIME, value
    if isinstance(value, UUID):
        return EdmType.GUID, value
    if isinstance(value, float):
        return EdmType.DOUBLE, value
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return EdmType.INT32, value
        if -(2**63) <= value < 2**63:
            return EdmType.INT64, value
        raise SerializationError("integer is outside EDM Int64 range")
    raise SerializationError(f"unsupported property type: {type(value).__name__}")


def encode_property(value: Any) -> dict[str, object]:
    if isinstance(value, EntityProperty):
        edm = EdmType(value.edm_type)
        raw = value.value
    else:
        edm, raw = _infer(value)
    tag = _TYPE_TAGS[edm]
    if edm == EdmType.BINARY:
        encoded: object = base64.b64encode(raw).decode("ascii")
    elif edm == EdmType.DATETIME:
        encoded = _iso_datetime(raw)
    elif edm == EdmType.GUID:
        encoded = str(raw).lower()
    elif edm == EdmType.DOUBLE:
        if not math.isfinite(raw):
            raise SerializationError("non-finite doubles are not supported by JSON")
        encoded = raw
    else:
        encoded = raw
    return {"type": tag, "value": encoded}


def decode_property(encoded: Mapping[str, Any]) -> EntityProperty:
    tag = encoded.get("type")
    if tag not in _TAG_TYPES:
        raise SerializationError(f"unsupported EDM type tag: {tag!r}")
    edm = _TAG_TYPES[tag]
    value = encoded.get("value")
    if edm == EdmType.BINARY:
        if not isinstance(value, str):
            raise SerializationError("Binary value must be a base64 string")
        try:
            value = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise SerializationError("invalid Binary encoding") from exc
    elif edm == EdmType.DATETIME:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise SerializationError("DateTime must be canonical UTC")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif edm == EdmType.GUID:
        value = UUID(value)
    return EntityProperty(value, edm)


def _entity_document(entity: Mapping[str, Any]) -> dict[str, object]:
    properties = {str(name): encode_property(value) for name, value in entity.items()}
    return {"properties": properties, "version": 1}


def encode_entity(entity: Mapping[str, Any]) -> bytes:
    """Encode one entity as one canonical UTF-8 JSON line."""
    return _canonical_json(_entity_document(entity)) + b"\n"


def encode_entity_ascii(entity: Mapping[str, Any]) -> bytes:
    """Encode a worst-case-safe ASCII-escaped representation for wire-size estimates."""
    return (
        json.dumps(
            _entity_document(entity), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
        + b"\n"
    )


def encode_entity_key(entity: Mapping[str, Any]) -> bytes:
    """Encode an entity key deterministically for streaming target verification."""
    try:
        key = {
            "PartitionKey": encode_property(entity["PartitionKey"]),
            "RowKey": encode_property(entity["RowKey"]),
        }
    except KeyError as exc:
        raise SerializationError("entity is missing PartitionKey or RowKey") from exc
    return _canonical_json(key) + b"\n"


def encode_entity_content(entity: Mapping[str, Any]) -> bytes:
    """Encode persisted entity content without the service-managed Timestamp."""
    properties = {
        str(name): encode_property(value) for name, value in entity.items() if name != "Timestamp"
    }
    return _canonical_json({"properties": properties, "version": 1}) + b"\n"


def decode_entity(line: bytes) -> dict[str, Any]:
    try:
        document = json.loads(line)
        if document.get("version") != 1 or not isinstance(document.get("properties"), dict):
            raise SerializationError("invalid entity record")
        entity: dict[str, Any] = {
            name: decode_property(value) for name, value in document["properties"].items()
        }
        for key_name in ("PartitionKey", "RowKey"):
            key = entity.get(key_name)
            if (
                not isinstance(key, EntityProperty)
                or key.edm_type != EdmType.STRING
                or not isinstance(key.value, str)
            ):
                raise SerializationError(f"{key_name} must be an EDM String")
            entity[key_name] = key.value
        return entity
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SerializationError("invalid entity JSON") from exc
