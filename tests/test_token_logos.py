import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest


def test_token_logo_cache_enforces_byte_budget_and_negative_ttl():
    from services import TokenLogo, TokenLogoCache

    cache = TokenLogoCache(
        max_bytes=5,
        max_entries=3,
        ttl_seconds=300,
        negative_ttl_seconds=10,
    )
    cache.set("a", TokenLogo(b"1234", "image/png"), now=100)
    cache.set("b", TokenLogo(b"12", "image/png"), now=101)

    assert cache.get("a", now=102) == (False, None)
    assert cache.get("b", now=102) == (True, TokenLogo(b"12", "image/png"))
    assert cache.total_bytes == 2

    cache.set("missing", None, now=103)
    assert cache.get("missing", now=112) == (True, None)
    assert cache.get("missing", now=114) == (False, None)
    assert cache.total_bytes == 2


def test_token_logo_cache_serializes_cross_thread_mutations(monkeypatch):
    from services import TokenLogo, TokenLogoCache

    cache = TokenLogoCache(max_bytes=64, max_entries=64)
    original_remove = cache._remove
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def observed_remove(key):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.002)
        try:
            return original_remove(key)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(cache, "_remove", observed_remove)
    logo = TokenLogo(content=b"12345678", content_type="image/png")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: cache.set(str(index), logo), range(32)))

    assert maximum_active == 1
    assert cache.total_bytes <= 64


def test_validated_dexscreener_logo_url_accepts_only_the_expected_cdn():
    from services import _token_logo_url, _validated_dexscreener_logo_url

    assert _validated_dexscreener_logo_url(
        "https://cdn.dexscreener.com/cms/images/token?width=800&format=auto"
    ) == "https://cdn.dexscreener.com/cms/images/token?width=800&format=auto"
    assert _validated_dexscreener_logo_url("http://cdn.dexscreener.com/cms/images/token") is None
    assert _validated_dexscreener_logo_url("https://cdn.dexscreener.com.evil.test/token.png") is None
    assert _validated_dexscreener_logo_url("https://user@cdn.dexscreener.com/token.png") is None
    assert _validated_dexscreener_logo_url("https://cdn.dexscreener.com:444/token.png") is None
    assert _validated_dexscreener_logo_url(None) is None

    pair = {
        "baseToken": {"address": "base-mint"},
        "quoteToken": {"address": "quote-mint"},
        "info": {"imageUrl": "https://cdn.dexscreener.com/cms/images/base-token"},
        "liquidity": {"usd": 1000},
    }
    assert _token_logo_url([pair], "base-mint") == pair["info"]["imageUrl"]
    assert _token_logo_url([pair], "quote-mint") is None


@pytest.mark.asyncio
async def test_fetch_token_logo_rejects_invalid_mints_without_network_access():
    from services import fetch_token_logo

    session = AsyncMock()
    assert await fetch_token_logo("not-a-mint", session=session) is None
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_logo_requests_coalesce_and_negative_results_are_cached(monkeypatch):
    import services

    mint = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
    services.token_logo_cache.clear()
    services._token_logo_inflight.clear()
    upstream = AsyncMock(return_value=None)
    monkeypatch.setattr(services, "_fetch_token_logo_uncached", upstream)

    assert await asyncio.gather(
        services.fetch_token_logo(mint),
        services.fetch_token_logo(mint),
    ) == [None, None]
    assert await services.fetch_token_logo(mint) is None
    assert upstream.await_count == 1


@pytest.mark.asyncio
async def test_logo_fetches_have_a_global_concurrency_bound(monkeypatch):
    import services

    services.token_logo_cache.clear()
    services._token_logo_inflight.clear()
    active = 0
    maximum_active = 0

    async def upstream(mint, session):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return None

    monkeypatch.setattr(services, "_fetch_token_logo_uncached", upstream)
    mints = ["1" * 31 + character for character in "23456789ABCDEFGHJKLM"]
    await asyncio.gather(*(services.fetch_token_logo(mint) for mint in mints))

    assert maximum_active <= services.MAX_CONCURRENT_TOKEN_LOGOS


@pytest.mark.asyncio
async def test_distinct_logo_jobs_have_a_bounded_admission_queue(monkeypatch):
    import services

    services.token_logo_cache.clear()
    services._token_logo_inflight.clear()
    release = asyncio.Event()

    async def upstream(mint, session):
        await release.wait()
        return None

    monkeypatch.setattr(services, "_fetch_token_logo_uncached", upstream)
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    count = services.MAX_PENDING_TOKEN_LOGOS + 8
    mints = ["1" * 31 + alphabet[index] for index in range(count)]
    requests = [asyncio.create_task(services.fetch_token_logo(mint)) for mint in mints]
    await asyncio.sleep(0.02)

    assert len(services._token_logo_inflight) == services.MAX_PENDING_TOKEN_LOGOS
    assert sum(request.done() for request in requests) == 8

    release.set()
    assert await asyncio.gather(*requests) == [None] * count
    await asyncio.sleep(0)
    assert services._token_logo_inflight == {}
