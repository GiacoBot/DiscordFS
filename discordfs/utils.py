"""Utility functions: hashing, UUID generation, instance ID, logging."""

from __future__ import annotations

import hashlib
import logging
import socket
import uuid


def file_sha256(data: bytes) -> str:
    """Return the hex SHA-256 digest of data."""
    return hashlib.sha256(data).hexdigest()


def new_file_uuid() -> str:
    """Generate a new UUID4 string for a file."""
    return str(uuid.uuid4())


def generate_instance_id() -> str:
    """Generate a unique instance ID: hostname-uuid4[:8]."""
    hostname = socket.gethostname()
    short_uuid = str(uuid.uuid4())[:8]
    return f"{hostname}-{short_uuid}"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure structured logging."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
