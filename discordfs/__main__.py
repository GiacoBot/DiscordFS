"""CLI entry point for DiscordFS."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from pathlib import Path

import click

from .config import load_config
from .db import Database
from .discord_store import DiscordStore
from .fs import LRUCache, mount_fs
from .sync import SyncManager
from .utils import generate_instance_id, setup_logging

log = logging.getLogger(__name__)


def _run_async_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run the asyncio event loop in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def _get_or_create_instance_id(db: Database) -> str:
    """Get the instance ID from DB, or generate and store a new one."""
    instance_id = await db.get_sync_meta("instance_id")
    if not instance_id:
        instance_id = generate_instance_id()
        await db.set_sync_meta("instance_id", instance_id)
        log.info("Generated new instance ID: %s", instance_id)
    else:
        log.info("Using instance ID: %s", instance_id)
    return instance_id


@click.group()
@click.option("--env", default=None, help="Path to .env file")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, env: str | None, verbose: bool) -> None:
    """DiscordFS - Cloud storage backed by Discord."""
    setup_logging(logging.DEBUG if verbose else logging.INFO)
    ctx.ensure_object(dict)
    ctx.obj["env"] = env


@cli.command()
@click.option("--mount", "mount_point", default=None, help="Mount point directory")
@click.option("--foreground/--no-foreground", default=True, help="Run in foreground")
@click.option("--no-sync", is_flag=True, help="Disable multi-instance sync")
@click.pass_context
def mount(ctx: click.Context, mount_point: str | None, foreground: bool, no_sync: bool) -> None:
    """Mount the DiscordFS filesystem."""
    config = load_config(ctx.obj["env"])
    mp = mount_point or config.mount_point

    # Create mount point if it doesn't exist
    Path(mp).mkdir(parents=True, exist_ok=True)

    # Create asyncio event loop in a background thread
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=_run_async_loop, args=(loop,), daemon=True)
    loop_thread.start()

    # Initialize DB
    db = asyncio.run_coroutine_threadsafe(Database(config.db_path).init(), loop).result(timeout=10)

    # Get or create instance ID
    instance_id = asyncio.run_coroutine_threadsafe(
        _get_or_create_instance_id(db), loop
    ).result(timeout=5)

    # Initialize Discord store with instance ID
    store = DiscordStore(config.bot_token, config.channel_id, config.password, instance_id)

    # Start the Discord client
    async def start_bot():
        await store.client.login(config.bot_token)

    asyncio.run_coroutine_threadsafe(start_bot(), loop).result(timeout=30)

    cache = LRUCache(config.cache_path, config.cache_max_bytes)

    # Set up sync manager
    sync_mgr: SyncManager | None = None
    sync_enabled = config.sync_enabled and not no_sync

    if sync_enabled:
        sync_mgr = SyncManager(db, store, cache, config.password, instance_id, config.sync_interval)

        # Run initial sync (full rebuild if DB is empty)
        recovered = asyncio.run_coroutine_threadsafe(
            sync_mgr.initial_sync(), loop
        ).result(timeout=300)
        if recovered > 0:
            log.info("Initial sync recovered %d files", recovered)

        # Start background sync polling
        asyncio.run_coroutine_threadsafe(sync_mgr.start(), loop).result(timeout=5)

    log.info("DiscordFS ready. Mounting at %s (sync=%s)", mp, "on" if sync_enabled else "off")

    # Handle graceful shutdown
    def shutdown(signum, frame):
        log.info("Shutting down...")
        if sync_mgr:
            asyncio.run_coroutine_threadsafe(sync_mgr.stop(), loop).result(timeout=10)
        asyncio.run_coroutine_threadsafe(db.close(), loop).result(timeout=5)
        asyncio.run_coroutine_threadsafe(store.client.close(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Mount FUSE (blocks in main thread)
    try:
        mount_fs(db, store, config.password, config.chunk_size, cache, mp, loop, foreground)
    finally:
        if sync_mgr:
            asyncio.run_coroutine_threadsafe(sync_mgr.stop(), loop).result(timeout=10)
        asyncio.run_coroutine_threadsafe(db.close(), loop).result(timeout=5)
        asyncio.run_coroutine_threadsafe(store.client.close(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)


@cli.command()
@click.pass_context
def rebuild(ctx: click.Context) -> None:
    """Rebuild local DB by scanning the Discord channel."""
    config = load_config(ctx.obj["env"])

    async def do_rebuild():
        db = await Database(config.db_path).init()
        store = DiscordStore(config.bot_token, config.channel_id, config.password)
        await store.client.login(config.bot_token)

        try:
            click.echo("Scanning Discord channel for DFS messages...")
            chunks_raw, metas_raw, deletes_raw = await store.scan_all_messages()

            # Resolve manifests
            manifests = []
            for m in metas_raw:
                if "attachment" in m:
                    resolved = await store.resolve_manifest(m)
                    if resolved:
                        manifests.append(resolved)
                else:
                    manifests.append(m)

            # Filter out deleted files
            deleted_uuids = {d["file_uuid"] for d in deletes_raw}
            chunks = [c for c in chunks_raw if c["file_uuid"] not in deleted_uuids]
            manifests = [m for m in manifests if m["file_uuid"] not in deleted_uuids]

            click.echo(f"Found {len(chunks)} chunks, {len(manifests)} manifests, {len(deletes_raw)} tombstones")

            recovered = await db.rebuild_from_scan(chunks, manifests)
            click.echo(f"Rebuilt DB: {recovered} files recovered")
        finally:
            await db.close()
            await store.client.close()

    asyncio.run(do_rebuild())


@cli.command()
@click.pass_context
def info(ctx: click.Context) -> None:
    """Show filesystem statistics."""
    config = load_config(ctx.obj["env"])

    async def do_info():
        db = await Database(config.db_path).init()
        try:
            async with db.db.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM files") as cur:
                row = await cur.fetchone()
                file_count, total_size = row

            async with db.db.execute("SELECT COUNT(*) FROM chunks") as cur:
                chunk_count = (await cur.fetchone())[0]

            async with db.db.execute("SELECT COUNT(*) FROM dirs") as cur:
                dir_count = (await cur.fetchone())[0]

            instance_id = await db.get_sync_meta("instance_id")
            last_sync = await db.get_sync_meta("last_known_msg_id")

            click.echo(f"Files:       {file_count}")
            click.echo(f"Directories: {dir_count}")
            click.echo(f"Chunks:      {chunk_count}")
            click.echo(f"Total size:  {total_size / (1024*1024):.2f} MB")
            click.echo(f"DB path:     {config.db_path}")
            click.echo(f"Channel:     {config.channel_id}")
            click.echo(f"Instance:    {instance_id or 'not set'}")
            click.echo(f"Last sync:   msg {last_sync or 'never'}")
        finally:
            await db.close()

    asyncio.run(do_info())


if __name__ == "__main__":
    cli()
