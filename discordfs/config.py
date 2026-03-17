"""Configuration loading from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: int
    password: str
    chunk_size: int = 8_388_608  # 8 MB
    mount_point: str = "./mnt"
    db_path: str = "./discordfs.db"
    cache_dir: str = "~/.discordfs/cache"
    cache_max_mb: int = 500

    @property
    def cache_path(self) -> Path:
        return Path(self.cache_dir).expanduser()

    @property
    def cache_max_bytes(self) -> int:
        return self.cache_max_mb * 1024 * 1024


def load_config(env_path: str | None = None) -> Config:
    """Load configuration from .env file and environment variables."""
    load_dotenv(env_path)

    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    channel_id_str = os.environ.get("DISCORD_CHANNEL_ID", "0")
    password = os.environ.get("DISCORDFS_PASSWORD", "")

    if not bot_token:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    if not password:
        raise ValueError("DISCORDFS_PASSWORD is required")

    return Config(
        bot_token=bot_token,
        channel_id=int(channel_id_str),
        password=password,
        chunk_size=int(os.environ.get("CHUNK_SIZE", "8388608")),
        mount_point=os.environ.get("MOUNT_POINT", "./mnt"),
        db_path=os.environ.get("DB_PATH", "./discordfs.db"),
        cache_dir=os.environ.get("CACHE_DIR", "~/.discordfs/cache"),
        cache_max_mb=int(os.environ.get("CACHE_MAX_MB", "500")),
    )
