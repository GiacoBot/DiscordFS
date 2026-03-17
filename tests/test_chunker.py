"""Tests for chunker module: split and join."""

import os

from discordfs.chunker import join_chunks, split_chunks


def test_split_exact():
    data = b"AABBCC"
    chunks = split_chunks(data, 2)
    assert chunks == [b"AA", b"BB", b"CC"]


def test_split_remainder():
    data = b"AABBCCD"
    chunks = split_chunks(data, 2)
    assert chunks == [b"AA", b"BB", b"CC", b"D"]


def test_split_single_chunk():
    data = b"small"
    chunks = split_chunks(data, 1024)
    assert chunks == [b"small"]


def test_split_empty():
    chunks = split_chunks(b"", 1024)
    assert chunks == []


def test_roundtrip():
    data = os.urandom(10_000)
    chunks = split_chunks(data, 3000)
    assert len(chunks) == 4
    assert join_chunks(chunks) == data


def test_split_invalid_size():
    try:
        split_chunks(b"data", 0)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
