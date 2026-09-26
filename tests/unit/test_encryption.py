import json
from unittest.mock import Mock

import pytest
from cryptography.exceptions import InvalidTag

from cosmos_table_backup.encryption import (
    CryptoError,
    NonceFactory,
    ObjectEncryptor,
    canonical_json,
    decrypt_chunks,
    decrypt_object,
    make_bootstrap,
    unwrap_dek,
    wrap_dek_once,
)


class Sink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)


def test_golden_aes_256_gcm_vector_and_roundtrip() -> None:
    key = bytes(range(32))
    nonce = bytes(range(12))
    aad = b'{"fixture":"v1"}'
    sink = Sink()
    encryptor = ObjectEncryptor(key, nonce, aad, sink)
    encryptor.write(b"cosmos-")
    encryptor.write(b"table-backup\n")
    result = encryptor.finalize()
    assert sink.data.hex() == (
        "4354424531000102030405060708090a0b246da576aa96ef6fec23fbee9c8b190e"
        "e8a3f73e1db08552d4a78b64a4d560f418f8a159"
    )
    assert result.byte_count == len(sink.data)
    assert decrypt_object(key, bytes(sink.data), aad) == b"cosmos-table-backup\n"


@pytest.mark.parametrize("mutation", ["ciphertext", "tag", "aad"])
def test_tampering_is_detected(mutation: str) -> None:
    sink = Sink()
    encryptor = ObjectEncryptor(b"k" * 32, b"n" * 12, b"aad", sink)
    encryptor.write(b"secret")
    encryptor.finalize()
    encoded = bytes(sink.data)
    aad = b"aad"
    if mutation == "aad":
        aad = b"changed"
    else:
        changed = bytearray(encoded)
        changed[-17 if mutation == "ciphertext" else -1] ^= 1
        encoded = bytes(changed)
    with pytest.raises(InvalidTag):
        decrypt_object(b"k" * 32, encoded, aad)


def test_bootstrap_is_minimal_canonical_aad_and_wraps_once() -> None:
    client = Mock()
    client.wrap_key.return_value.encrypted_key = b"wrapped"
    wrapped = wrap_dek_once(client, b"d" * 32)
    assert wrapped == b"wrapped"
    assert client.wrap_key.call_count == 1
    bootstrap = make_bootstrap("id", "https://kv.vault.azure.net/keys/k/v", wrapped, b"n" * 12)
    assert set(bootstrap) == {
        "backup_id",
        "bootstrap_version",
        "encrypted_manifest",
        "key_id",
        "manifest_nonce",
        "object_format",
        "wrap_algorithm",
        "wrapped_dek",
    }
    assert json.loads(canonical_json(bootstrap)) == bootstrap


def test_bounded_chunk_decryption_and_unwrap() -> None:
    sink = Sink()
    encryptor = ObjectEncryptor(b"k" * 32, b"n" * 12, b"aad", sink)
    encryptor.write(b"plaintext")
    encryptor.finalize()
    output = Sink()
    pieces = [bytes(sink.data[index : index + 2]) for index in range(0, len(sink.data), 2)]
    encrypted_hash, plaintext_hash, byte_count = decrypt_chunks(
        b"k" * 32, pieces, b"aad", output.write, expected_nonce=b"n" * 12
    )
    assert bytes(output.data) == b"plaintext"
    assert len(encrypted_hash) == len(plaintext_hash) == 64
    assert byte_count == len(sink.data)
    client = Mock()
    client.unwrap_key.return_value.key = b"k" * 32
    assert unwrap_dek(client, b"wrapped") == b"k" * 32
    assert client.unwrap_key.call_count == 1


def test_nonce_factory_rejects_reuse_and_encryptor_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter([b"x" * 12, b"x" * 12, b"y" * 12])
    monkeypatch.setattr("cosmos_table_backup.encryption.os.urandom", lambda size: next(values))
    factory = NonceFactory()
    assert factory.generate() == b"x" * 12
    assert factory.generate() == b"y" * 12
    with pytest.raises(CryptoError):
        ObjectEncryptor(b"short", b"n" * 12, b"", Sink())
    encryptor = ObjectEncryptor(b"k" * 32, b"n" * 12, b"", Sink())
    encryptor.finalize()
    with pytest.raises(CryptoError):
        encryptor.write(b"x")
