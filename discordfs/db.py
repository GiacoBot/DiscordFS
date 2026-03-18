"""SQLite database for tracking files, chunks, directories, manifests, and sync state."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Self

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_uuid    TEXT PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    size_bytes   INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    total_chunks INTEGER NOT NULL,
    created_at   REAL NOT NULL,
    modified_at  REAL NOT NULL,
    mode         INTEGER NOT NULL DEFAULT 33188
);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_uuid       TEXT NOT NULL REFERENCES files(file_uuid) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    discord_msg_id  TEXT NOT NULL,
    discord_att_url TEXT,
    size_bytes      INTEGER NOT NULL,
    uploaded_at     REAL NOT NULL,
    UNIQUE(file_uuid, chunk_index)
);

CREATE TABLE IF NOT EXISTS dirs (
    path        TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    mode        INTEGER NOT NULL DEFAULT 16877
);

CREATE TABLE IF NOT EXISTS manifests (
    file_uuid      TEXT PRIMARY KEY REFERENCES files(file_uuid) ON DELETE CASCADE,
    discord_msg_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_uuid);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
"""


@dataclass
class FileRow:
    file_uuid: str
    path: str
    size_bytes: int
    sha256: str
    total_chunks: int
    created_at: float
    modified_at: float
    mode: int


@dataclass
class ChunkRow:
    id: int
    file_uuid: str
    chunk_index: int
    discord_msg_id: str
    discord_att_url: str | None
    size_bytes: int
    uploaded_at: float


@dataclass
class DirRow:
    path: str
    created_at: float
    mode: int


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> Self:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not initialized"
        return self._db

    # ── Transaction support ──────────────────────────────────────

    @asynccontextmanager
    async def transaction(self):
        """Execute a block inside BEGIN IMMEDIATE/COMMIT, with ROLLBACK on error."""
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    # ── File operations ──────────────────────────────────────────

    async def add_file(
        self,
        file_uuid: str,
        path: str,
        size_bytes: int,
        sha256: str,
        total_chunks: int,
        mode: int = 0o100644,
        *,
        auto_commit: bool = True,
    ) -> None:
        now = time.time()
        await self.db.execute(
            "INSERT INTO files (file_uuid, path, size_bytes, sha256, total_chunks, created_at, modified_at, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_uuid, path, size_bytes, sha256, total_chunks, now, now, mode),
        )
        if auto_commit:
            await self.db.commit()

    async def get_file(self, path: str) -> FileRow | None:
        async with self.db.execute(
            "SELECT file_uuid, path, size_bytes, sha256, total_chunks, created_at, modified_at, mode "
            "FROM files WHERE path = ?",
            (path,),
        ) as cur:
            row = await cur.fetchone()
            return FileRow(*row) if row else None

    async def get_file_by_uuid(self, file_uuid: str) -> FileRow | None:
        async with self.db.execute(
            "SELECT file_uuid, path, size_bytes, sha256, total_chunks, created_at, modified_at, mode "
            "FROM files WHERE file_uuid = ?",
            (file_uuid,),
        ) as cur:
            row = await cur.fetchone()
            return FileRow(*row) if row else None

    async def update_file(
        self,
        path: str,
        size_bytes: int,
        sha256: str,
        total_chunks: int,
        *,
        auto_commit: bool = True,
    ) -> None:
        now = time.time()
        await self.db.execute(
            "UPDATE files SET size_bytes=?, sha256=?, total_chunks=?, modified_at=? WHERE path=?",
            (size_bytes, sha256, total_chunks, now, path),
        )
        if auto_commit:
            await self.db.commit()

    async def delete_file(self, path: str, *, auto_commit: bool = True) -> list[str]:
        """Delete a file and return its Discord message IDs (chunks + manifest)."""
        file = await self.get_file(path)
        if not file:
            return []

        msg_ids: list[str] = []

        # Collect chunk message IDs
        async with self.db.execute(
            "SELECT discord_msg_id FROM chunks WHERE file_uuid = ?",
            (file.file_uuid,),
        ) as cur:
            async for row in cur:
                msg_ids.append(row[0])

        # Collect manifest message ID
        async with self.db.execute(
            "SELECT discord_msg_id FROM manifests WHERE file_uuid = ?",
            (file.file_uuid,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                msg_ids.append(row[0])

        # CASCADE handles chunks and manifests
        await self.db.execute("DELETE FROM files WHERE file_uuid = ?", (file.file_uuid,))
        if auto_commit:
            await self.db.commit()
        return msg_ids

    async def rename_file(self, old_path: str, new_path: str, *, auto_commit: bool = True) -> None:
        await self.db.execute(
            "UPDATE files SET path = ?, modified_at = ? WHERE path = ?",
            (new_path, time.time(), old_path),
        )
        if auto_commit:
            await self.db.commit()

    async def list_dir(self, dir_path: str) -> list[str]:
        """List immediate children (files and dirs) of a directory path.

        Returns basenames, not full paths.
        """
        if dir_path == "/":
            prefix = "/"
        else:
            prefix = dir_path.rstrip("/") + "/"

        entries: set[str] = set()

        # Files directly in this directory
        async with self.db.execute("SELECT path FROM files") as cur:
            async for (fpath,) in cur:
                if fpath.startswith(prefix):
                    relative = fpath[len(prefix) :]
                    # Only immediate children (no '/' in relative path)
                    if "/" not in relative and relative:
                        entries.add(relative)

        # Subdirectories
        async with self.db.execute("SELECT path FROM dirs") as cur:
            async for (dpath,) in cur:
                if dpath.startswith(prefix):
                    relative = dpath[len(prefix) :]
                    if "/" not in relative and relative:
                        entries.add(relative)

        return sorted(entries)

    # ── Chunk operations ─────────────────────────────────────────

    async def add_chunk(
        self,
        file_uuid: str,
        chunk_index: int,
        discord_msg_id: str,
        discord_att_url: str | None,
        size_bytes: int,
        *,
        auto_commit: bool = True,
    ) -> None:
        await self.db.execute(
            "INSERT INTO chunks (file_uuid, chunk_index, discord_msg_id, discord_att_url, size_bytes, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (file_uuid, chunk_index, discord_msg_id, discord_att_url, size_bytes, time.time()),
        )
        if auto_commit:
            await self.db.commit()

    async def get_chunks(self, file_uuid: str) -> list[ChunkRow]:
        async with self.db.execute(
            "SELECT id, file_uuid, chunk_index, discord_msg_id, discord_att_url, size_bytes, uploaded_at "
            "FROM chunks WHERE file_uuid = ? ORDER BY chunk_index",
            (file_uuid,),
        ) as cur:
            rows = await cur.fetchall()
            return [ChunkRow(*r) for r in rows]

    async def delete_chunks(self, file_uuid: str, *, auto_commit: bool = True) -> list[str]:
        """Delete all chunks for a file, returning their Discord message IDs."""
        msg_ids: list[str] = []
        async with self.db.execute(
            "SELECT discord_msg_id FROM chunks WHERE file_uuid = ?",
            (file_uuid,),
        ) as cur:
            async for row in cur:
                msg_ids.append(row[0])
        await self.db.execute("DELETE FROM chunks WHERE file_uuid = ?", (file_uuid,))
        if auto_commit:
            await self.db.commit()
        return msg_ids

    # ── Directory operations ─────────────────────────────────────

    async def add_dir(self, path: str, mode: int = 0o40755, *, auto_commit: bool = True) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO dirs (path, created_at, mode) VALUES (?, ?, ?)",
            (path, time.time(), mode),
        )
        if auto_commit:
            await self.db.commit()

    async def remove_dir(self, path: str, *, auto_commit: bool = True) -> None:
        await self.db.execute("DELETE FROM dirs WHERE path = ?", (path,))
        if auto_commit:
            await self.db.commit()

    async def dir_exists(self, path: str) -> bool:
        if path == "/":
            return True
        async with self.db.execute(
            "SELECT 1 FROM dirs WHERE path = ?", (path,)
        ) as cur:
            return await cur.fetchone() is not None

    async def get_dir(self, path: str) -> DirRow | None:
        if path == "/":
            return DirRow(path="/", created_at=0.0, mode=0o40755)
        async with self.db.execute(
            "SELECT path, created_at, mode FROM dirs WHERE path = ?", (path,)
        ) as cur:
            row = await cur.fetchone()
            return DirRow(*row) if row else None

    async def ensure_parent_dirs(self, path: str) -> None:
        """Create all parent directories for a given path."""
        parts = PurePosixPath(path).parents
        for p in reversed(list(parts)):
            p_str = str(p)
            if p_str != "/":
                await self.add_dir(p_str, auto_commit=False)

    # ── Manifest operations ──────────────────────────────────────

    async def add_manifest(self, file_uuid: str, discord_msg_id: str, *, auto_commit: bool = True) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO manifests (file_uuid, discord_msg_id) VALUES (?, ?)",
            (file_uuid, discord_msg_id),
        )
        if auto_commit:
            await self.db.commit()

    async def get_manifest_msg_id(self, file_uuid: str) -> str | None:
        async with self.db.execute(
            "SELECT discord_msg_id FROM manifests WHERE file_uuid = ?",
            (file_uuid,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    # ── Sync metadata operations ─────────────────────────────────

    async def get_sync_meta(self, key: str) -> str | None:
        async with self.db.execute(
            "SELECT value FROM sync_meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_sync_meta(self, key: str, value: str, *, auto_commit: bool = True) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO sync_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        if auto_commit:
            await self.db.commit()

    async def file_count(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM files") as cur:
            return (await cur.fetchone())[0]

    # ── Rebuild from Discord scan ────────────────────────────────

    async def rebuild_from_scan(
        self,
        chunks: list[dict],
        manifests: list[dict],
    ) -> int:
        """Rebuild the database from scanned Discord messages.

        Returns the number of files recovered.
        """
        async with self.transaction():
            # Clear existing data (except sync_meta)
            await self.db.execute("DELETE FROM manifests")
            await self.db.execute("DELETE FROM chunks")
            await self.db.execute("DELETE FROM dirs")
            await self.db.execute("DELETE FROM files")

            # Group chunks by file_uuid
            files_map: dict[str, list[dict]] = {}
            for c in chunks:
                files_map.setdefault(c["file_uuid"], []).append(c)

            # Build manifest lookup
            manifest_map: dict[str, dict] = {}
            for m in manifests:
                manifest_map[m["file_uuid"]] = m

            recovered = 0
            for file_uuid, file_chunks in files_map.items():
                file_chunks.sort(key=lambda c: c["chunk_index"])
                first = file_chunks[0]

                meta = manifest_map.get(file_uuid)
                if meta:
                    path = meta["path"]
                    size = meta.get("size", 0)
                    mode = meta.get("mode", 0o100644)
                else:
                    path = f"/recovered/{file_uuid}"
                    size = 0
                    mode = 0o100644

                await self.ensure_parent_dirs(path)

                await self.db.execute(
                    "INSERT OR REPLACE INTO files (file_uuid, path, size_bytes, sha256, total_chunks, created_at, modified_at, mode) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (file_uuid, path, size, first["sha256"], first["total_chunks"], first["ts"], first["ts"], mode),
                )

                for c in file_chunks:
                    await self.db.execute(
                        "INSERT OR REPLACE INTO chunks (file_uuid, chunk_index, discord_msg_id, discord_att_url, size_bytes, uploaded_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (c["file_uuid"], c["chunk_index"], c["discord_msg_id"], c.get("att_url"), c["size_bytes"], c["ts"]),
                    )

                if meta:
                    await self.db.execute(
                        "INSERT OR REPLACE INTO manifests (file_uuid, discord_msg_id) VALUES (?, ?)",
                        (file_uuid, meta["discord_msg_id"]),
                    )

                recovered += 1

        return recovered
