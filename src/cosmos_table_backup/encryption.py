"""Versioned AES-256-GCM object encryption and Key Vault wrapping."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from azure.keyvault.keys.crypto import KeyWrapAlgorithm
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

OBJECT_MAGIC = b"CTBE1"
NONCE_SIZE = 12
TAG_SIZE = 16


class CryptoError(RuntimeError):
    """Raised when cryptographic invariants cannot be met."""


class ChunkSink(Protocol):
    def write(self, data: bytes) -> None: ...


class AsyncChunkSink(Protocol):
    async def write(self, data: bytes) -> None: ...


class _PendingChunks:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)


@dataclass(frozen=True, slots=True)
class EncryptionResult:
    nonce: bytes
    sha256: str
    byte_count: int


class NonceFactory:
    """Generate nonces unique within one DEK lifetime."""

    def __init__(self) -> None:
        self._used: set[bytes] = set()

    def generate(self) -> bytes:
        for _ in range(32):
            nonce = os.urandom(NONCE_SIZE)
            if nonce not in self._used:
                self._used.add(nonce)
                return nonce
        raise CryptoError("unable to generate a unique nonce")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


class ObjectEncryptor:
    """Incrementally encrypt one independently authenticated object."""

    def __init__(self, dek: bytes, nonce: bytes, aad: bytes, sink: ChunkSink) -> None:
        if len(dek) != 32 or len(nonce) != NONCE_SIZE:
            raise CryptoError("AES-256-GCM requires a 32-byte key and 12-byte nonce")
        self._ctx = Cipher(algorithms.AES(dek), modes.GCM(nonce)).encryptor()
        self._ctx.authenticate_additional_data(aad)
        self._sink = sink
        self._hash = hashlib.sha256()
        self._count = 0
        self._closed = False
        self._nonce = nonce
        self._emit(OBJECT_MAGIC + nonce)

    def _emit(self, data: bytes) -> None:
        if data:
            self._sink.write(data)
            self._hash.update(data)
            self._count += len(data)

    def write(self, plaintext: bytes) -> None:
        if self._closed:
            raise CryptoError("encryptor is finalized")
        self._emit(self._ctx.update(plaintext))

    def finalize(self) -> EncryptionResult:
        if self._closed:
            raise CryptoError("encryptor is finalized")
        self._emit(self._ctx.finalize())
        self._emit(self._ctx.tag)
        self._closed = True
        return EncryptionResult(self._nonce, self._hash.hexdigest(), self._count)


class AsyncObjectEncryptor:
    """Reuse the v1 encryptor with at most one producer call pending at a time."""

    def __init__(self, dek: bytes, nonce: bytes, aad: bytes, sink: AsyncChunkSink) -> None:
        self._pending = _PendingChunks()
        self._encryptor = ObjectEncryptor(dek, nonce, aad, self._pending)
        self._sink = sink

    async def _drain(self) -> None:
        while self._pending.chunks:
            chunk = self._pending.chunks.pop(0)
            await self._sink.write(chunk)

    async def write(self, plaintext: bytes) -> None:
        await self._drain()
        self._encryptor.write(plaintext)
        await self._drain()

    async def finalize(self) -> EncryptionResult:
        await self._drain()
        result = self._encryptor.finalize()
        await self._drain()
        return result


def decrypt_object(dek: bytes, encoded: bytes, aad: bytes) -> bytes:
    """Reference decryption used by contract tests and future restore tooling."""
    if len(encoded) < len(OBJECT_MAGIC) + NONCE_SIZE + TAG_SIZE or not encoded.startswith(
        OBJECT_MAGIC
    ):
        raise CryptoError("invalid encrypted object framing")
    offset = len(OBJECT_MAGIC)
    nonce = encoded[offset : offset + NONCE_SIZE]
    body = encoded[offset + NONCE_SIZE : -TAG_SIZE]
    tag = encoded[-TAG_SIZE:]
    decryptor = Cipher(algorithms.AES(dek), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(aad)
    return decryptor.update(body) + decryptor.finalize()


def _start_chunk_decryptor(
    dek: bytes,
    pending: bytearray,
    aad: bytes,
    expected_nonce: bytes | None,
) -> Any:
    if not pending.startswith(OBJECT_MAGIC):
        raise CryptoError("invalid encrypted object framing")
    offset = len(OBJECT_MAGIC)
    nonce = bytes(pending[offset : offset + NONCE_SIZE])
    if expected_nonce is not None and nonce != expected_nonce:
        raise CryptoError("encrypted object nonce does not match bootstrap")
    del pending[: offset + NONCE_SIZE]
    decryptor = Cipher(algorithms.AES(dek), modes.GCM(nonce)).decryptor()
    decryptor.authenticate_additional_data(aad)
    return decryptor


def _decrypt_pending(
    decryptor: Any,
    pending: bytearray,
    plaintext_hash: Any,
    emit: Callable[[bytes], None],
) -> None:
    if len(pending) <= TAG_SIZE:
        return
    ciphertext = bytes(pending[:-TAG_SIZE])
    del pending[:-TAG_SIZE]
    plaintext = decryptor.update(ciphertext)
    plaintext_hash.update(plaintext)
    emit(plaintext)


def decrypt_chunks(
    dek: bytes,
    chunks: Iterable[bytes],
    aad: bytes,
    emit: Callable[[bytes], None],
    *,
    expected_nonce: bytes | None = None,
) -> tuple[str, str, int]:
    """Authenticate a framed object while consuming bounded forward-only chunks."""
    if len(dek) != 32:
        raise CryptoError("AES-256-GCM requires a 32-byte key")
    pending = bytearray()
    decryptor: Any | None = None
    encrypted_hash = hashlib.sha256()
    plaintext_hash = hashlib.sha256()
    byte_count = 0
    for chunk in chunks:
        encrypted_hash.update(chunk)
        byte_count += len(chunk)
        pending.extend(chunk)
        if decryptor is None and len(pending) >= len(OBJECT_MAGIC) + NONCE_SIZE:
            decryptor = _start_chunk_decryptor(dek, pending, aad, expected_nonce)
        if decryptor is not None:
            _decrypt_pending(decryptor, pending, plaintext_hash, emit)
    if decryptor is None or len(pending) != TAG_SIZE:
        raise CryptoError("truncated encrypted object")
    final = decryptor.finalize_with_tag(bytes(pending))
    plaintext_hash.update(final)
    emit(final)
    return encrypted_hash.hexdigest(), plaintext_hash.hexdigest(), byte_count


def generate_dek() -> bytes:
    return os.urandom(32)


def wrap_dek_once(crypto_client: Any, dek: bytes) -> bytes:
    result = crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek)
    return _wrapped_key(result)


async def wrap_dek_once_async(crypto_client: Any, dek: bytes) -> bytes:
    result = await crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek)
    return _wrapped_key(result)


def _wrapped_key(result: Any) -> bytes:
    wrapped = bytes(result.encrypted_key)
    if not wrapped:
        raise CryptoError("Key Vault returned an empty wrapped key")
    return wrapped


def unwrap_dek(crypto_client: Any, wrapped_dek: bytes) -> bytes:
    result = crypto_client.unwrap_key(KeyWrapAlgorithm.rsa_oaep_256, wrapped_dek)
    dek = bytes(result.key)
    if len(dek) != 32:
        raise CryptoError("Key Vault returned an invalid AES-256 key")
    return dek


def make_bootstrap(
    backup_id: str, key_id: str, wrapped_dek: bytes, manifest_nonce: bytes
) -> dict[str, object]:
    """Return only the public metadata required to locate and decrypt the manifest."""
    if len(manifest_nonce) != NONCE_SIZE:
        raise CryptoError("manifest nonce must be 96 bits")
    return {
        "backup_id": backup_id,
        "bootstrap_version": 1,
        "encrypted_manifest": "manifest.enc",
        "key_id": key_id,
        "manifest_nonce": base64.b64encode(manifest_nonce).decode("ascii"),
        "object_format": 1,
        "wrap_algorithm": "RSA-OAEP-256",
        "wrapped_dek": base64.b64encode(wrapped_dek).decode("ascii"),
    }
