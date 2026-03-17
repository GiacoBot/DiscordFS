"""Discord I/O layer: upload, download, delete chunks and manifests."""

from __future__ import annotations

import io
import json
import logging
import re
import time
from typing import Any

import discord
import pyzipper

log = logging.getLogger(__name__)

# Regex for parsing DFS messages
_CHUNK_RE = re.compile(
    r"^\[DFS:v1\] file=(?P<file_uuid>[0-9a-f-]+) chunk=(?P<idx>\d+)/(?P<total>\d+) "
    r"sha256=(?P<sha256>[0-9a-f]+) ts=(?P<ts>\d+)$"
)
_META_RE = re.compile(
    r"^\[DFS:v1:meta\] file=(?P<file_uuid>[0-9a-f-]+) ts=(?P<ts>\d+)$"
)


def _format_chunk_message(
    file_uuid: str,
    chunk_index: int,
    total_chunks: int,
    sha256: str,
) -> str:
    ts = int(time.time())
    return f"[DFS:v1] file={file_uuid} chunk={chunk_index}/{total_chunks} sha256={sha256} ts={ts}"


def _format_meta_message(file_uuid: str) -> str:
    ts = int(time.time())
    return f"[DFS:v1:meta] file={file_uuid} ts={ts}"


def _encrypt_manifest(metadata: dict, password: str) -> bytes:
    """Encrypt manifest JSON with AES-256 zip."""
    buf = io.BytesIO()
    with pyzipper.AESZipFile(
        buf, "w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr("meta.json", json.dumps(metadata).encode("utf-8"))
    return buf.getvalue()


def _decrypt_manifest(data: bytes, password: str) -> dict:
    """Decrypt manifest JSON from AES-256 zip."""
    buf = io.BytesIO(data)
    with pyzipper.AESZipFile(buf, "r") as zf:
        zf.setpassword(password.encode("utf-8"))
        return json.loads(zf.read("meta.json"))


class DiscordStore:
    """Handles all Discord API interactions for file storage."""

    def __init__(self, token: str, channel_id: int, password: str) -> None:
        self._token = token
        self._channel_id = channel_id
        self._password = password
        self._client: discord.Client | None = None
        self._channel: discord.TextChannel | None = None

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

    @property
    def client(self) -> discord.Client:
        assert self._client is not None
        return self._client

    async def ensure_channel(self) -> discord.TextChannel:
        """Get the configured channel, fetching it if needed."""
        if self._channel is None:
            ch = self.client.get_channel(self._channel_id)
            if ch is None:
                ch = await self.client.fetch_channel(self._channel_id)
            assert isinstance(ch, discord.TextChannel)
            self._channel = ch
        return self._channel

    async def upload_chunk(
        self,
        file_uuid: str,
        chunk_index: int,
        total_chunks: int,
        sha256: str,
        data: bytes,
    ) -> tuple[str, str | None]:
        """Upload one chunk as a Discord message attachment.

        Returns (message_id, attachment_url).
        """
        channel = await self.ensure_channel()
        content = _format_chunk_message(file_uuid, chunk_index, total_chunks, sha256)
        filename = f"{file_uuid}_{chunk_index}.bin"

        attachment = discord.File(io.BytesIO(data), filename=filename)
        msg = await channel.send(content=content, file=attachment)

        att_url = msg.attachments[0].url if msg.attachments else None
        log.info("Uploaded chunk %d/%d for %s (msg=%s)", chunk_index, total_chunks, file_uuid, msg.id)
        return str(msg.id), att_url

    async def upload_manifest(
        self,
        file_uuid: str,
        path: str,
        size: int,
        mode: int,
    ) -> str:
        """Upload an encrypted manifest message. Returns message_id."""
        channel = await self.ensure_channel()
        content = _format_meta_message(file_uuid)

        metadata = {"path": path, "size": size, "mode": mode}
        encrypted = _encrypt_manifest(metadata, self._password)

        attachment = discord.File(io.BytesIO(encrypted), filename=f"{file_uuid}_meta.bin")
        msg = await channel.send(content=content, file=attachment)

        log.info("Uploaded manifest for %s (msg=%s)", file_uuid, msg.id)
        return str(msg.id)

    async def download_chunk(self, message_id: str) -> bytes:
        """Download a chunk by fetching the message and its attachment."""
        channel = await self.ensure_channel()
        msg = await channel.fetch_message(int(message_id))
        if not msg.attachments:
            raise ValueError(f"Message {message_id} has no attachments")
        return await msg.attachments[0].read()

    async def delete_messages(self, message_ids: list[str]) -> None:
        """Delete Discord messages by ID."""
        channel = await self.ensure_channel()
        for msg_id in message_ids:
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.delete()
                log.info("Deleted message %s", msg_id)
            except discord.NotFound:
                log.warning("Message %s already deleted", msg_id)
            except discord.HTTPException as e:
                log.error("Failed to delete message %s: %s", msg_id, e)

    async def scan_all_messages(self) -> tuple[list[dict], list[dict]]:
        """Scan entire channel history for DFS messages.

        Returns (chunks_list, manifests_list) where each item is a dict
        with parsed metadata suitable for Database.rebuild_from_scan().
        """
        channel = await self.ensure_channel()
        chunks: list[dict] = []
        manifests: list[dict] = []
        count = 0

        async for msg in channel.history(limit=None, oldest_first=True):
            count += 1
            if not msg.content:
                continue

            # Try chunk message
            m = _CHUNK_RE.match(msg.content)
            if m and msg.attachments:
                chunks.append({
                    "file_uuid": m.group("file_uuid"),
                    "chunk_index": int(m.group("idx")),
                    "total_chunks": int(m.group("total")),
                    "sha256": m.group("sha256"),
                    "ts": float(m.group("ts")),
                    "discord_msg_id": str(msg.id),
                    "att_url": msg.attachments[0].url,
                    "size_bytes": msg.attachments[0].size,
                })
                continue

            # Try manifest message
            m = _META_RE.match(msg.content)
            if m and msg.attachments:
                try:
                    att_data = await msg.attachments[0].read()
                    meta = _decrypt_manifest(att_data, self._password)
                    manifests.append({
                        "file_uuid": m.group("file_uuid"),
                        "discord_msg_id": str(msg.id),
                        "ts": float(m.group("ts")),
                        **meta,
                    })
                except Exception:
                    log.warning("Failed to decrypt manifest for msg %s", msg.id)

        log.info("Scanned %d messages: %d chunks, %d manifests", count, len(chunks), len(manifests))
        return chunks, manifests
