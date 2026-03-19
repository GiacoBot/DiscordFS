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

from .retry import retry_discord

log = logging.getLogger(__name__)

# Regex for parsing DFS messages
# [DFS] file=<uuid> chunk=1/3 sha256=<hash> ts=<ts> by=<instance_id>
_CHUNK_RE = re.compile(
    r"^\[DFS\] file=(?P<file_uuid>[0-9a-f-]+) chunk=(?P<idx>\d+)/(?P<total>\d+) "
    r"sha256=(?P<sha256>[0-9a-f]+) ts=(?P<ts>\d+)(?:\s+by=(?P<by>\S+))?$"
)

# [DFS:meta] file=<uuid> ts=<ts> by=<instance_id>
_META_RE = re.compile(
    r"^\[DFS:meta\] file=(?P<file_uuid>[0-9a-f-]+) ts=(?P<ts>\d+)(?:\s+by=(?P<by>\S+))?$"
)

# [DFS:delete] file=<uuid> ts=<ts> by=<instance_id>
_DELETE_RE = re.compile(
    r"^\[DFS:delete\] file=(?P<file_uuid>[0-9a-f-]+) ts=(?P<ts>\d+) by=(?P<by>\S+)$"
)


def _format_chunk_message(
    file_uuid: str,
    chunk_index: int,
    total_chunks: int,
    sha256: str,
    instance_id: str,
) -> str:
    ts = int(time.time())
    return f"[DFS] file={file_uuid} chunk={chunk_index}/{total_chunks} sha256={sha256} ts={ts} by={instance_id}"


def _format_meta_message(file_uuid: str, instance_id: str) -> str:
    ts = int(time.time())
    return f"[DFS:meta] file={file_uuid} ts={ts} by={instance_id}"


def _format_delete_message(file_uuid: str, instance_id: str) -> str:
    ts = int(time.time())
    return f"[DFS:delete] file={file_uuid} ts={ts} by={instance_id}"


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

    def __init__(self, token: str, channel_id: int, password: str, instance_id: str = "") -> None:
        self._token = token
        self._channel_id = channel_id
        self._password = password
        self._instance_id = instance_id
        self._client: discord.Client | None = None
        self._channel: discord.TextChannel | None = None

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @instance_id.setter
    def instance_id(self, value: str) -> None:
        self._instance_id = value

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
        content = _format_chunk_message(file_uuid, chunk_index, total_chunks, sha256, self._instance_id)
        filename = f"{file_uuid}_{chunk_index}.bin"

        async def _send():
            attachment = discord.File(io.BytesIO(data), filename=filename)
            return await channel.send(content=content, file=attachment)

        msg = await retry_discord(_send)

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
        content = _format_meta_message(file_uuid, self._instance_id)

        metadata = {"path": path, "size": size, "mode": mode}
        encrypted = _encrypt_manifest(metadata, self._password)

        async def _send():
            attachment = discord.File(io.BytesIO(encrypted), filename=f"{file_uuid}_meta.bin")
            return await channel.send(content=content, file=attachment)

        msg = await retry_discord(_send)

        log.info("Uploaded manifest for %s (msg=%s)", file_uuid, msg.id)
        return str(msg.id)

    async def send_tombstone(self, file_uuid: str) -> str:
        """Post a deletion tombstone message. Returns message_id."""
        channel = await self.ensure_channel()
        content = _format_delete_message(file_uuid, self._instance_id)

        async def _send():
            return await channel.send(content=content)

        msg = await retry_discord(_send)
        log.info("Posted tombstone for %s (msg=%s)", file_uuid, msg.id)
        return str(msg.id)

    async def download_chunk(self, message_id: str) -> bytes:
        """Download a chunk by fetching the message and its attachment."""
        channel = await self.ensure_channel()

        async def _fetch():
            msg = await channel.fetch_message(int(message_id))
            if not msg.attachments:
                raise ValueError(f"Message {message_id} has no attachments")
            return await msg.attachments[0].read()

        return await retry_discord(_fetch)

    async def delete_messages(self, message_ids: list[str]) -> None:
        """Delete Discord messages by ID."""
        channel = await self.ensure_channel()
        for msg_id in message_ids:
            try:
                async def _delete(mid=msg_id):
                    msg = await channel.fetch_message(int(mid))
                    await msg.delete()

                await retry_discord(_delete)
                log.info("Deleted message %s", msg_id)
            except discord.NotFound:
                log.warning("Message %s already deleted", msg_id)
            except discord.Forbidden:
                log.error("No permission to delete message %s", msg_id)

    async def scan_all_messages(self) -> tuple[list[dict], list[dict], list[dict]]:
        """Scan entire channel history for DFS messages.

        Returns (chunks_list, manifests_list, deletes_list).
        """
        channel = await self.ensure_channel()
        chunks: list[dict] = []
        manifests: list[dict] = []
        deletes: list[dict] = []
        count = 0

        async for msg in channel.history(limit=None, oldest_first=True):
            count += 1
            if not msg.content:
                continue

            parsed = self._parse_message(msg)
            if parsed:
                msg_type, data = parsed
                if msg_type == "chunk":
                    chunks.append(data)
                elif msg_type == "meta":
                    manifests.append(data)
                elif msg_type == "delete":
                    deletes.append(data)

        log.info("Scanned %d messages: %d chunks, %d manifests, %d deletes", count, len(chunks), len(manifests), len(deletes))
        return chunks, manifests, deletes

    async def scan_messages_after(self, after_msg_id: str | None) -> tuple[list[dict], list[dict], list[dict], str | None]:
        """Scan channel messages after a given message ID (incremental sync).

        Returns (chunks, manifests, deletes, last_msg_id).
        """
        channel = await self.ensure_channel()
        chunks: list[dict] = []
        manifests: list[dict] = []
        deletes: list[dict] = []
        last_msg_id: str | None = after_msg_id

        after = discord.Object(id=int(after_msg_id)) if after_msg_id else None

        async for msg in channel.history(limit=None, oldest_first=True, after=after):
            last_msg_id = str(msg.id)

            if not msg.content:
                continue

            parsed = self._parse_message(msg)
            if parsed:
                msg_type, data = parsed
                if msg_type == "chunk":
                    chunks.append(data)
                elif msg_type == "meta":
                    manifests.append(data)
                elif msg_type == "delete":
                    deletes.append(data)

        return chunks, manifests, deletes, last_msg_id

    def _parse_message(self, msg: discord.Message) -> tuple[str, dict] | None:
        """Parse a Discord message into a typed dict, or None if not a DFS message."""
        content = msg.content

        # Try chunk message
        m = _CHUNK_RE.match(content)
        if m and msg.attachments:
            return "chunk", {
                "file_uuid": m.group("file_uuid"),
                "chunk_index": int(m.group("idx")),
                "total_chunks": int(m.group("total")),
                "sha256": m.group("sha256"),
                "ts": float(m.group("ts")),
                "discord_msg_id": str(msg.id),
                "att_url": msg.attachments[0].url,
                "size_bytes": msg.attachments[0].size,
                "by": m.group("by") or "",
            }

        # Try manifest message
        m = _META_RE.match(content)
        if m and msg.attachments:
            try:
                return "meta", {
                    "file_uuid": m.group("file_uuid"),
                    "discord_msg_id": str(msg.id),
                    "ts": float(m.group("ts")),
                    "by": m.group("by") or "",
                    "attachment": msg.attachments[0],
                }
            except Exception:
                log.warning("Failed to parse manifest for msg %s", msg.id)
                return None

        # Try delete/tombstone message
        m = _DELETE_RE.match(content)
        if m:
            return "delete", {
                "file_uuid": m.group("file_uuid"),
                "ts": float(m.group("ts")),
                "discord_msg_id": str(msg.id),
                "by": m.group("by") or "",
            }

        return None

    async def resolve_manifest(self, pending: dict) -> dict | None:
        """Download and decrypt a manifest attachment. Returns full manifest dict or None."""
        try:
            att_data = await pending["attachment"].read()
            meta = _decrypt_manifest(att_data, self._password)
            result = {
                "file_uuid": pending["file_uuid"],
                "discord_msg_id": pending["discord_msg_id"],
                "ts": pending["ts"],
                "by": pending["by"],
                **meta,
            }
            return result
        except Exception:
            log.warning("Failed to decrypt manifest for msg %s", pending["discord_msg_id"])
            return None
