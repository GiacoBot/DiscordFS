"""FUSE filesystem implementation using fusepy."""

from __future__ import annotations

import asyncio
import errno
import io
import logging
import os
import stat
import time
from pathlib import Path

from fuse import FUSE, FuseOSError, Operations

from .chunker import join_chunks, split_chunks
from .crypto import decrypt_data, encrypt_data
from .db import Database
from .discord_store import DiscordStore
from .utils import file_sha256, new_file_uuid

log = logging.getLogger(__name__)


class LRUCache:
    """Simple LRU disk cache for decrypted files."""

    def __init__(self, cache_dir: Path, max_bytes: int) -> None:
        self._dir = cache_dir
        self._max_bytes = max_bytes
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, file_uuid: str, sha256: str) -> Path:
        return self._dir / f"{file_uuid}_{sha256}.bin"

    def get(self, file_uuid: str, sha256: str) -> bytes | None:
        p = self._path(file_uuid, sha256)
        if p.exists():
            # Touch to update access time (LRU)
            p.touch()
            return p.read_bytes()
        return None

    def put(self, file_uuid: str, sha256: str, data: bytes) -> None:
        self._evict_if_needed(len(data))
        p = self._path(file_uuid, sha256)
        p.write_bytes(data)

    def invalidate(self, file_uuid: str) -> None:
        for p in self._dir.glob(f"{file_uuid}_*.bin"):
            p.unlink(missing_ok=True)

    def _evict_if_needed(self, incoming_size: int) -> None:
        """Evict oldest files until there's room."""
        files = sorted(self._dir.iterdir(), key=lambda p: p.stat().st_atime)
        total = sum(p.stat().st_size for p in files) + incoming_size

        while total > self._max_bytes and files:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)


class WriteBuffer:
    """In-memory buffer for file writes (write-on-close semantics)."""

    def __init__(self) -> None:
        self.buf = io.BytesIO()
        self.dirty = False

    def write(self, data: bytes, offset: int) -> int:
        self.buf.seek(offset)
        self.buf.write(data)
        self.dirty = True
        return len(data)

    def read(self, size: int, offset: int) -> bytes:
        self.buf.seek(offset)
        return self.buf.read(size)

    def truncate(self, length: int) -> None:
        self.buf.truncate(length)
        self.dirty = True

    def getvalue(self) -> bytes:
        return self.buf.getvalue()

    def size(self) -> int:
        pos = self.buf.tell()
        self.buf.seek(0, 2)
        s = self.buf.tell()
        self.buf.seek(pos)
        return s


class DiscordFSOperations(Operations):
    """FUSE operations backed by Discord storage."""

    def __init__(
        self,
        db: Database,
        store: DiscordStore,
        password: str,
        chunk_size: int,
        cache: LRUCache,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._db = db
        self._store = store
        self._password = password
        self._chunk_size = chunk_size
        self._cache = cache
        self._loop = loop
        self._next_fh = 1
        self._open_files: dict[int, WriteBuffer] = {}
        self._read_cache: dict[int, bytes] = {}  # fh -> decrypted content

    def _run(self, coro):
        """Run an async coroutine from the sync FUSE thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=120)

    def _alloc_fh(self) -> int:
        fh = self._next_fh
        self._next_fh += 1
        return fh

    # ── Helpers ──────────────────────────────────────────────────

    def _download_and_decrypt(self, file_uuid: str, sha256: str) -> bytes:
        """Download all chunks, reassemble, and decrypt a file."""
        # Check cache first
        cached = self._cache.get(file_uuid, sha256)
        if cached is not None:
            log.debug("Cache hit for %s", file_uuid)
            return cached

        chunks_rows = self._run(self._db.get_chunks(file_uuid))
        if not chunks_rows:
            raise FuseOSError(errno.EIO)

        raw_chunks = []
        for cr in chunks_rows:
            data = self._run(self._store.download_chunk(cr.discord_msg_id))
            raw_chunks.append(data)

        encrypted = join_chunks(raw_chunks)
        decrypted = decrypt_data(encrypted, self._password)

        # Store in cache
        self._cache.put(file_uuid, sha256, decrypted)
        return decrypted

    def _upload_file(self, file_uuid: str, path: str, data: bytes, mode: int) -> None:
        """Encrypt, chunk, and upload a file to Discord."""
        sha256 = file_sha256(data)
        encrypted = encrypt_data(data, self._password)
        chunks = split_chunks(encrypted, self._chunk_size)
        total = len(chunks)

        # Delete old chunks if file already exists
        old_msg_ids = self._run(self._db.delete_chunks(file_uuid))
        old_manifest_id = self._run(self._db.get_manifest_msg_id(file_uuid))
        if old_manifest_id:
            old_msg_ids.append(old_manifest_id)
        if old_msg_ids:
            self._run(self._store.delete_messages(old_msg_ids))

        # Upload new chunks
        for i, chunk_data in enumerate(chunks):
            msg_id, att_url = self._run(
                self._store.upload_chunk(file_uuid, i, total, sha256, chunk_data)
            )
            self._run(self._db.add_chunk(file_uuid, i, msg_id, att_url, len(chunk_data)))

        # Upload manifest
        manifest_msg_id = self._run(
            self._store.upload_manifest(file_uuid, path, len(data), mode)
        )
        self._run(self._db.add_manifest(file_uuid, manifest_msg_id))

        # Update file record
        self._run(self._db.update_file(path, len(data), sha256, total))

        # Update cache
        self._cache.invalidate(file_uuid)
        self._cache.put(file_uuid, sha256, data)

    # ── Filesystem info ──────────────────────────────────────────

    def statfs(self, path):
        return {
            "f_bsize": 4096,
            "f_frsize": 4096,
            "f_blocks": 1024 * 1024,
            "f_bfree": 1024 * 1024,
            "f_bavail": 1024 * 1024,
            "f_files": 0,
            "f_ffree": 0,
            "f_favail": 0,
            "f_namemax": 255,
        }

    # ── Attribute operations ─────────────────────────────────────

    def getattr(self, path, fh=None):
        # Check if it's a file being written
        if fh and fh in self._open_files:
            wb = self._open_files[fh]
            now = time.time()
            return {
                "st_mode": stat.S_IFREG | 0o644,
                "st_nlink": 1,
                "st_size": wb.size(),
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
                "st_uid": os.getuid(),
                "st_gid": os.getgid(),
            }

        # Check if it's a directory
        dir_row = self._run(self._db.get_dir(path))
        if dir_row:
            return {
                "st_mode": dir_row.mode,
                "st_nlink": 2,
                "st_size": 0,
                "st_ctime": dir_row.created_at,
                "st_mtime": dir_row.created_at,
                "st_atime": dir_row.created_at,
                "st_uid": os.getuid(),
                "st_gid": os.getgid(),
            }

        # Check if it's a file
        file_row = self._run(self._db.get_file(path))
        if file_row:
            return {
                "st_mode": file_row.mode,
                "st_nlink": 1,
                "st_size": file_row.size_bytes,
                "st_ctime": file_row.created_at,
                "st_mtime": file_row.modified_at,
                "st_atime": file_row.modified_at,
                "st_uid": os.getuid(),
                "st_gid": os.getgid(),
            }

        raise FuseOSError(errno.ENOENT)

    # ── Directory operations ─────────────────────────────────────

    def readdir(self, path, fh):
        entries = [".", ".."]
        entries.extend(self._run(self._db.list_dir(path)))
        return entries

    def mkdir(self, path, mode):
        # Ensure parent exists
        parent = str(Path(path).parent)
        if parent != "/" and not self._run(self._db.dir_exists(parent)):
            raise FuseOSError(errno.ENOENT)
        self._run(self._db.add_dir(path, stat.S_IFDIR | (mode & 0o7777)))

    def rmdir(self, path):
        children = self._run(self._db.list_dir(path))
        if children:
            raise FuseOSError(errno.ENOTEMPTY)
        self._run(self._db.remove_dir(path))

    # ── File read operations ─────────────────────────────────────

    def open(self, path, flags):
        file_row = self._run(self._db.get_file(path))
        if not file_row:
            raise FuseOSError(errno.ENOENT)

        fh = self._alloc_fh()

        # If opening for writing, prepare a write buffer with existing content
        if flags & (os.O_WRONLY | os.O_RDWR):
            wb = WriteBuffer()
            if file_row.size_bytes > 0:
                content = self._download_and_decrypt(file_row.file_uuid, file_row.sha256)
                wb.buf.write(content)
                wb.buf.seek(0)
            self._open_files[fh] = wb

        return fh

    def read(self, path, size, offset, fh):
        # Check write buffer first
        if fh in self._open_files:
            return self._open_files[fh].read(size, offset)

        # Check read cache
        if fh in self._read_cache:
            data = self._read_cache[fh]
            return data[offset : offset + size]

        # Download and cache for this handle
        file_row = self._run(self._db.get_file(path))
        if not file_row:
            raise FuseOSError(errno.ENOENT)

        data = self._download_and_decrypt(file_row.file_uuid, file_row.sha256)
        self._read_cache[fh] = data
        return data[offset : offset + size]

    def release(self, path, fh):
        # Flush write buffer if dirty
        if fh in self._open_files:
            wb = self._open_files[fh]
            if wb.dirty:
                file_row = self._run(self._db.get_file(path))
                if file_row:
                    self._upload_file(
                        file_row.file_uuid, path, wb.getvalue(), file_row.mode
                    )
            del self._open_files[fh]

        # Clean read cache
        self._read_cache.pop(fh, None)

    # ── File write operations ────────────────────────────────────

    def create(self, path, mode, fi=None):
        # Ensure parent directory exists
        parent = str(Path(path).parent)
        if parent != "/" and not self._run(self._db.dir_exists(parent)):
            raise FuseOSError(errno.ENOENT)

        file_uuid = new_file_uuid()
        self._run(
            self._db.add_file(
                file_uuid=file_uuid,
                path=path,
                size_bytes=0,
                sha256="",
                total_chunks=0,
                mode=stat.S_IFREG | (mode & 0o7777),
            )
        )

        fh = self._alloc_fh()
        self._open_files[fh] = WriteBuffer()
        return fh

    def write(self, path, data, offset, fh):
        if fh not in self._open_files:
            raise FuseOSError(errno.EBADF)
        return self._open_files[fh].write(data, offset)

    def flush(self, path, fh):
        # Actual upload happens on release
        pass

    def truncate(self, path, length, fh=None):
        if fh and fh in self._open_files:
            self._open_files[fh].truncate(length)
            return

        file_row = self._run(self._db.get_file(path))
        if not file_row:
            raise FuseOSError(errno.ENOENT)

        if length == 0:
            # Truncate to zero: delete chunks, update DB
            msg_ids = self._run(self._db.delete_chunks(file_row.file_uuid))
            if msg_ids:
                self._run(self._store.delete_messages(msg_ids))
            self._run(self._db.update_file(path, 0, "", 0))
            self._cache.invalidate(file_row.file_uuid)

    # ── File delete ──────────────────────────────────────────────

    def unlink(self, path):
        file_row = self._run(self._db.get_file(path))
        if not file_row:
            raise FuseOSError(errno.ENOENT)

        msg_ids = self._run(self._db.delete_file(path))
        if msg_ids:
            self._run(self._store.delete_messages(msg_ids))

        self._cache.invalidate(file_row.file_uuid)

    # ── Rename ───────────────────────────────────────────────────

    def rename(self, old, new):
        # Check source exists
        file_row = self._run(self._db.get_file(old))
        if file_row:
            # If destination already exists, remove it first (POSIX rename semantics)
            existing = self._run(self._db.get_file(new))
            if existing:
                msg_ids = self._run(self._db.delete_file(new))
                if msg_ids:
                    self._run(self._store.delete_messages(msg_ids))
                self._cache.invalidate(existing.file_uuid)

            # Update manifest on Discord with new path
            old_manifest_id = self._run(self._db.get_manifest_msg_id(file_row.file_uuid))
            if old_manifest_id:
                self._run(self._store.delete_messages([old_manifest_id]))
            new_manifest_id = self._run(
                self._store.upload_manifest(file_row.file_uuid, new, file_row.size_bytes, file_row.mode)
            )
            self._run(self._db.add_manifest(file_row.file_uuid, new_manifest_id))
            self._run(self._db.rename_file(old, new))
            return

        # Maybe it's a directory rename
        if self._run(self._db.dir_exists(old)):
            # Simple dir rename — update path in dirs table
            self._run(self._db.remove_dir(old))
            self._run(self._db.add_dir(new))
            return

        raise FuseOSError(errno.ENOENT)

    # ── Permission stubs ─────────────────────────────────────────

    def chmod(self, path, mode):
        pass

    def chown(self, path, uid, gid):
        pass

    def utimens(self, path, times=None):
        pass


def mount_fs(
    db: Database,
    store: DiscordStore,
    password: str,
    chunk_size: int,
    cache: LRUCache,
    mount_point: str,
    loop: asyncio.AbstractEventLoop,
    foreground: bool = True,
) -> None:
    """Mount the FUSE filesystem."""
    ops = DiscordFSOperations(db, store, password, chunk_size, cache, loop)
    log.info("Mounting DiscordFS at %s", mount_point)
    FUSE(
        ops,
        mount_point,
        foreground=foreground,
        nothreads=False,
        allow_other=False,
        ro=False,
    )
