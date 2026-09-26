"""Private final backup manifest model."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from cosmos_table_backup.encryption import canonical_json


@dataclass(frozen=True, slots=True)
class TableManifest:
    table_name: str
    object_name: str
    entity_count: int
    encrypted_byte_count: int
    encrypted_sha256: str
    plaintext_sha256: str
    keys_sha256: str
    entity_content_sha256: str
    started_at: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_id: str
    application_version: str
    manifest_version: int
    consistency: str
    started_at: str
    completed_at: str
    tables: tuple[TableManifest, ...]

    def to_bytes(self) -> bytes:
        return canonical_json(asdict(self))
