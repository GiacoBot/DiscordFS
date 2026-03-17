"""Split and reassemble encrypted blobs into fixed-size chunks."""

from __future__ import annotations


def split_chunks(data: bytes, chunk_size: int) -> list[bytes]:
    """Split data into chunks of at most chunk_size bytes."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def join_chunks(chunks: list[bytes]) -> bytes:
    """Concatenate chunks back into a single blob."""
    return b"".join(chunks)
