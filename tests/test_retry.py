"""Tests for retry logic."""

import asyncio

import pytest

from discordfs.retry import retry_discord


class FakeHTTPException(Exception):
    def __init__(self, status, retry_after=None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}")


@pytest.mark.asyncio
async def test_succeeds_first_try():
    call_count = 0

    async def _op():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = await retry_discord(_op, max_retries=3, base_delay=0.01)
    assert result == "ok"
    assert call_count == 1


@pytest.mark.asyncio
async def test_retries_on_connection_error():
    call_count = 0

    async def _op():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("connection lost")
        return "recovered"

    result = await retry_discord(_op, max_retries=4, base_delay=0.01)
    assert result == "recovered"
    assert call_count == 3


@pytest.mark.asyncio
async def test_raises_after_max_retries():
    async def _op():
        raise ConnectionError("always fails")

    with pytest.raises(ConnectionError):
        await retry_discord(_op, max_retries=2, base_delay=0.01)


@pytest.mark.asyncio
async def test_does_not_retry_on_404():
    """discord.NotFound (404) should not be retried."""
    import discord

    call_count = 0

    async def _op():
        nonlocal call_count
        call_count += 1
        resp = type("Resp", (), {"status": 404, "reason": "Not Found"})()
        raise discord.NotFound(resp, "not found")

    with pytest.raises(discord.NotFound):
        await retry_discord(_op, max_retries=3, base_delay=0.01)
    assert call_count == 1


@pytest.mark.asyncio
async def test_retries_on_timeout():
    call_count = 0

    async def _op():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise asyncio.TimeoutError()
        return "ok"

    result = await retry_discord(_op, max_retries=3, base_delay=0.01)
    assert result == "ok"
    assert call_count == 2
