"""Retry logic with exponential backoff for Discord API calls."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import discord

log = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_discord(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 4,
    base_delay: float = 1.0,
) -> T:
    """Retry a Discord API call with exponential backoff and jitter.

    Args:
        coro_factory: A zero-arg callable that returns a new coroutine each attempt.
        max_retries: Maximum number of retries (excluding rate limit retries).
        base_delay: Base delay in seconds for exponential backoff.

    Returns:
        The result of the coroutine.

    Behavior:
        - HTTP 429 (rate limited): sleep for retry_after, does NOT count against max_retries.
        - HTTP 5xx, ConnectionError, TimeoutError: exponential backoff with jitter.
        - HTTP 4xx (except 429): raise immediately (not retryable).
        - After max_retries exhausted: raise the last exception.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except discord.HTTPException as e:
            if e.status == 429:
                # Rate limited — sleep and retry without counting
                retry_after = getattr(e, "retry_after", 5.0) or 5.0
                log.warning("Rate limited, retrying after %.1fs", retry_after)
                await asyncio.sleep(retry_after)
                continue
            elif e.status >= 500:
                # Server error — retryable
                last_exc = e
            elif e.status in (401, 403, 404):
                # Client error — not retryable
                raise
            else:
                last_exc = e
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            last_exc = e

        if attempt < max_retries:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning(
                "Discord API error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, max_retries, delay, last_exc,
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc
