"""Tests for LRU disk cache."""

import tempfile
from pathlib import Path

from discordfs.fs import LRUCache


def test_put_and_get():
    with tempfile.TemporaryDirectory() as d:
        cache = LRUCache(Path(d), max_bytes=1024 * 1024)
        cache.put("uuid1", "sha1", b"hello")
        assert cache.get("uuid1", "sha1") == b"hello"


def test_get_miss():
    with tempfile.TemporaryDirectory() as d:
        cache = LRUCache(Path(d), max_bytes=1024 * 1024)
        assert cache.get("nonexistent", "sha") is None


def test_invalidate():
    with tempfile.TemporaryDirectory() as d:
        cache = LRUCache(Path(d), max_bytes=1024 * 1024)
        cache.put("uuid2", "sha1", b"data1")
        cache.put("uuid2", "sha2", b"data2")
        cache.invalidate("uuid2")
        assert cache.get("uuid2", "sha1") is None
        assert cache.get("uuid2", "sha2") is None


def test_eviction():
    with tempfile.TemporaryDirectory() as d:
        cache = LRUCache(Path(d), max_bytes=100)
        cache.put("a", "s1", b"x" * 60)
        cache.put("b", "s2", b"y" * 60)
        # 'a' should be evicted since total would exceed 100
        assert cache.get("a", "s1") is None
        assert cache.get("b", "s2") == b"y" * 60


def test_different_sha_different_entries():
    with tempfile.TemporaryDirectory() as d:
        cache = LRUCache(Path(d), max_bytes=1024 * 1024)
        cache.put("uuid3", "old_sha", b"old data")
        cache.put("uuid3", "new_sha", b"new data")
        assert cache.get("uuid3", "old_sha") == b"old data"
        assert cache.get("uuid3", "new_sha") == b"new data"
