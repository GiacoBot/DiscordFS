"""Multi-instance synchronization via incremental Discord channel scan."""

from __future__ import annotations

import asyncio
import logging

from .db import Database
from .discord_store import DiscordStore
from .fs import LRUCache

log = logging.getLogger(__name__)


class SyncManager:
    """Background task that polls the Discord channel for changes from other instances."""

    def __init__(
        self,
        db: Database,
        store: DiscordStore,
        cache: LRUCache,
        password: str,
        instance_id: str,
        poll_interval: int = 30,
    ) -> None:
        self._db = db
        self._store = store
        self._cache = cache
        self._password = password
        self._instance_id = instance_id
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background polling task."""
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info("SyncManager started (interval=%ds, instance=%s)", self._poll_interval, self._instance_id)

    async def stop(self) -> None:
        """Stop the background polling task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("SyncManager stopped")

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        # Initial delay to let the system settle
        await asyncio.sleep(5)

        while self._running:
            try:
                await self._incremental_sync()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Sync error")

            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

    async def _incremental_sync(self) -> None:
        """Fetch new messages from Discord and update local DB."""
        last_msg_id = await self._db.get_sync_meta("last_known_msg_id")

        chunks, meta_pendings, deletes, new_last_msg_id = await self._store.scan_messages_after(last_msg_id)

        if not chunks and not meta_pendings and not deletes:
            # Nothing new
            if new_last_msg_id and new_last_msg_id != last_msg_id:
                await self._db.set_sync_meta("last_known_msg_id", new_last_msg_id)
            return

        # Filter out our own messages
        chunks = [c for c in chunks if c.get("by") != self._instance_id]
        meta_pendings = [m for m in meta_pendings if m.get("by") != self._instance_id]
        deletes = [d for d in deletes if d.get("by") != self._instance_id]

        if not chunks and not meta_pendings and not deletes:
            if new_last_msg_id:
                await self._db.set_sync_meta("last_known_msg_id", new_last_msg_id)
            return

        # Resolve manifest attachments
        manifests: list[dict] = []
        for pending in meta_pendings:
            if pending.get("attachment"):
                resolved = await self._store.resolve_manifest(pending)
                if resolved:
                    manifests.append(resolved)

        # Process deletions
        for d in deletes:
            file_uuid = d["file_uuid"]
            existing = await self._db.get_file_by_uuid(file_uuid)
            if existing:
                log.info("Sync: remote delete of %s (%s)", existing.path, file_uuid)
                await self._db.delete_file(existing.path)
                self._cache.invalidate(file_uuid)

        # Group new chunks by file_uuid
        files_map: dict[str, list[dict]] = {}
        for c in chunks:
            files_map.setdefault(c["file_uuid"], []).append(c)

        # Build manifest lookup
        manifest_map: dict[str, dict] = {}
        for m in manifests:
            manifest_map[m["file_uuid"]] = m

        # Process new/updated files
        for file_uuid, file_chunks in files_map.items():
            file_chunks.sort(key=lambda c: c["chunk_index"])
            first = file_chunks[0]

            existing = await self._db.get_file_by_uuid(file_uuid)
            if existing and existing.sha256 == first["sha256"]:
                # Same content, skip
                continue

            meta = manifest_map.get(file_uuid)
            if meta:
                path = meta["path"]
                size = meta.get("size", 0)
                mode = meta.get("mode", 0o100644)
            else:
                if existing:
                    # Keep existing path if no new manifest
                    path = existing.path
                    size = existing.size_bytes
                    mode = existing.mode
                else:
                    path = f"/recovered/{file_uuid}"
                    size = 0
                    mode = 0o100644

            if existing:
                log.info("Sync: remote update of %s (sha256 changed)", path)
                # Conflict: last-writer-wins
                if existing.sha256 != first["sha256"]:
                    log.warning(
                        "Sync conflict on %s: local=%s remote=%s (remote wins)",
                        path, existing.sha256[:12], first["sha256"][:12],
                    )
            else:
                log.info("Sync: new remote file %s", path)

            # Update DB in transaction
            async with self._db.transaction():
                if existing:
                    await self._db.delete_chunks(file_uuid, auto_commit=False)
                else:
                    await self._db.ensure_parent_dirs(path)
                    await self._db.add_file(
                        file_uuid, path, size, first["sha256"], first["total_chunks"], mode,
                        auto_commit=False,
                    )

                if existing:
                    await self._db.update_file(path, size, first["sha256"], first["total_chunks"], auto_commit=False)

                for c in file_chunks:
                    await self._db.add_chunk(
                        c["file_uuid"], c["chunk_index"], c["discord_msg_id"],
                        c.get("att_url"), c["size_bytes"],
                        auto_commit=False,
                    )

                if meta:
                    await self._db.add_manifest(file_uuid, meta["discord_msg_id"], auto_commit=False)

            # Invalidate cache for changed files
            self._cache.invalidate(file_uuid)

        # Update last known message ID
        if new_last_msg_id:
            await self._db.set_sync_meta("last_known_msg_id", new_last_msg_id)

        synced = len(files_map) + len(deletes)
        if synced > 0:
            log.info("Sync complete: %d files updated/added, %d deleted", len(files_map), len(deletes))

    async def initial_sync(self) -> int:
        """Run a full sync if the DB is empty. Returns number of files recovered."""
        file_count = await self._db.file_count()
        if file_count > 0:
            log.info("DB has %d files, skipping initial full sync", file_count)
            # Still set up incremental sync from the latest message
            last_msg_id = await self._db.get_sync_meta("last_known_msg_id")
            if not last_msg_id:
                # Scan to find the last message ID without rebuilding
                _, _, _, last_id = await self._store.scan_messages_after(None)
                if last_id:
                    await self._db.set_sync_meta("last_known_msg_id", last_id)
            return 0

        log.info("DB is empty, running full sync...")
        chunks_raw, metas_raw, deletes_raw = await self._store.scan_all_messages()

        # Resolve manifests from scan_all_messages (which returns fully parsed dicts)
        # scan_all_messages returns chunks and meta_pendings, need to resolve
        manifests: list[dict] = []
        for m in metas_raw:
            if "attachment" in m:
                resolved = await self._store.resolve_manifest(m)
                if resolved:
                    manifests.append(resolved)
            else:
                manifests.append(m)

        # Filter out deleted files
        deleted_uuids = {d["file_uuid"] for d in deletes_raw}
        chunks_raw = [c for c in chunks_raw if c["file_uuid"] not in deleted_uuids]
        manifests = [m for m in manifests if m["file_uuid"] not in deleted_uuids]

        recovered = await self._db.rebuild_from_scan(chunks_raw, manifests)

        # Store the last message ID
        if chunks_raw:
            all_msg_ids = [int(c["discord_msg_id"]) for c in chunks_raw]
            for m in manifests:
                all_msg_ids.append(int(m["discord_msg_id"]))
            for d in deletes_raw:
                all_msg_ids.append(int(d["discord_msg_id"]))
            last_id = str(max(all_msg_ids))
            await self._db.set_sync_meta("last_known_msg_id", last_id)

        log.info("Full sync complete: %d files recovered", recovered)
        return recovered
