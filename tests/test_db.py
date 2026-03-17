"""Tests for database module using in-memory SQLite."""

import pytest
import pytest_asyncio

from discordfs.db import Database


@pytest_asyncio.fixture
async def db():
    d = Database(":memory:")
    await d.init()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_add_and_get_file(db: Database):
    await db.add_file("uuid-1", "/test.txt", 100, "abc123", 1)
    f = await db.get_file("/test.txt")
    assert f is not None
    assert f.file_uuid == "uuid-1"
    assert f.size_bytes == 100
    assert f.sha256 == "abc123"
    assert f.total_chunks == 1


@pytest.mark.asyncio
async def test_get_nonexistent_file(db: Database):
    f = await db.get_file("/nope")
    assert f is None


@pytest.mark.asyncio
async def test_delete_file_returns_msg_ids(db: Database):
    await db.add_file("uuid-2", "/del.txt", 50, "hash", 2)
    await db.add_chunk("uuid-2", 0, "msg-100", None, 25)
    await db.add_chunk("uuid-2", 1, "msg-101", None, 25)
    await db.add_manifest("uuid-2", "msg-200")

    msg_ids = await db.delete_file("/del.txt")
    assert set(msg_ids) == {"msg-100", "msg-101", "msg-200"}

    f = await db.get_file("/del.txt")
    assert f is None


@pytest.mark.asyncio
async def test_list_dir(db: Database):
    await db.add_dir("/docs")
    await db.add_file("uuid-3", "/docs/a.txt", 10, "h1", 1)
    await db.add_file("uuid-4", "/docs/b.txt", 20, "h2", 1)
    await db.add_dir("/docs/sub")

    entries = await db.list_dir("/docs")
    assert sorted(entries) == ["a.txt", "b.txt", "sub"]


@pytest.mark.asyncio
async def test_list_root(db: Database):
    await db.add_file("uuid-5", "/readme.md", 5, "h", 1)
    await db.add_dir("/docs")

    entries = await db.list_dir("/")
    assert sorted(entries) == ["docs", "readme.md"]


@pytest.mark.asyncio
async def test_chunks_ordering(db: Database):
    await db.add_file("uuid-6", "/big.bin", 1000, "hash", 3)
    await db.add_chunk("uuid-6", 2, "msg-c", None, 100)
    await db.add_chunk("uuid-6", 0, "msg-a", None, 400)
    await db.add_chunk("uuid-6", 1, "msg-b", None, 500)

    chunks = await db.get_chunks("uuid-6")
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    assert [c.discord_msg_id for c in chunks] == ["msg-a", "msg-b", "msg-c"]


@pytest.mark.asyncio
async def test_dir_operations(db: Database):
    assert await db.dir_exists("/") is True
    assert await db.dir_exists("/foo") is False

    await db.add_dir("/foo")
    assert await db.dir_exists("/foo") is True

    await db.remove_dir("/foo")
    assert await db.dir_exists("/foo") is False


@pytest.mark.asyncio
async def test_rename_file(db: Database):
    await db.add_file("uuid-7", "/old.txt", 10, "h", 1)
    await db.rename_file("/old.txt", "/new.txt")

    assert await db.get_file("/old.txt") is None
    f = await db.get_file("/new.txt")
    assert f is not None
    assert f.file_uuid == "uuid-7"


@pytest.mark.asyncio
async def test_rebuild_from_scan(db: Database):
    chunks = [
        {"file_uuid": "uuid-r1", "chunk_index": 0, "total_chunks": 2, "sha256": "hash1", "ts": 1000.0, "discord_msg_id": "m1", "att_url": None, "size_bytes": 500},
        {"file_uuid": "uuid-r1", "chunk_index": 1, "total_chunks": 2, "sha256": "hash1", "ts": 1001.0, "discord_msg_id": "m2", "att_url": None, "size_bytes": 500},
        {"file_uuid": "uuid-r2", "chunk_index": 0, "total_chunks": 1, "sha256": "hash2", "ts": 2000.0, "discord_msg_id": "m3", "att_url": None, "size_bytes": 300},
    ]
    manifests = [
        {"file_uuid": "uuid-r1", "discord_msg_id": "m10", "ts": 1000.0, "path": "/docs/report.pdf", "size": 1000, "mode": 33188},
    ]

    recovered = await db.rebuild_from_scan(chunks, manifests)
    assert recovered == 2

    f1 = await db.get_file("/docs/report.pdf")
    assert f1 is not None
    assert f1.total_chunks == 2

    # uuid-r2 has no manifest, should be under /recovered/
    f2 = await db.get_file("/recovered/uuid-r2")
    assert f2 is not None
    assert f2.total_chunks == 1
