"""Tests for crypto module: encrypt/decrypt round-trip."""

import os

from discordfs.crypto import decrypt_data, encrypt_data


def test_roundtrip_small():
    data = b"Hello, DiscordFS!"
    password = "test_password_123"
    encrypted = encrypt_data(data, password)
    assert encrypted != data
    decrypted = decrypt_data(encrypted, password)
    assert decrypted == data


def test_roundtrip_empty():
    data = b""
    password = "pw"
    encrypted = encrypt_data(data, password)
    decrypted = decrypt_data(encrypted, password)
    assert decrypted == data


def test_roundtrip_large():
    data = os.urandom(10 * 1024 * 1024)  # 10 MB
    password = "strong_password"
    encrypted = encrypt_data(data, password)
    decrypted = decrypt_data(encrypted, password)
    assert decrypted == data


def test_compression_reduces_size():
    # Highly compressible data
    data = b"A" * 100_000
    password = "pw"
    encrypted = encrypt_data(data, password)
    assert len(encrypted) < len(data)


def test_wrong_password_fails():
    data = b"secret data"
    encrypted = encrypt_data(data, "correct")
    try:
        decrypt_data(encrypted, "wrong")
        assert False, "Should have raised an exception"
    except Exception:
        pass  # Expected
