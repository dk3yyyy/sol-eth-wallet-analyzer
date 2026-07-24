from unittest.mock import AsyncMock

import httpx
import pytest

from services import ServiceError


async def request(app, method, path, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def test_rate_limiter_enforces_window_without_persisting_payloads():
    from web_app import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)

    assert limiter.allow("client-a", now=100.0)
    assert limiter.allow("client-a", now=110.0)
    assert not limiter.allow("client-a", now=120.0)
    assert limiter.allow("client-b", now=120.0)
    assert limiter.allow("client-a", now=161.0)


SAMPLE_RESULT = {
    "address": "0x" + "1" * 40,
    "chain": "ethereum",
    "native_asset": {
        "symbol": "ETH",
        "balance": 1.0,
        "price_usd": 2_500.0,
        "value_usd": 2_500.0,
    },
    "total_value_usd": 2_500.0,
    "tokens": [],
    "token_summary": {
        "holding_count": 0,
        "valued_count": 0,
        "unavailable_count": 0,
        "value_usd": 0.0,
        "allocation_percent": 0.0,
    },
    "warnings": [],
    "explorer_url": "https://etherscan.io/address/0x" + "1" * 40,
    "updated_at": "2026-07-24T12:00:00Z",
}


@pytest.mark.asyncio
async def test_health_reports_supported_chains():
    from web_app import app

    response = await request(app, "GET", "/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "supported_chains": ["solana", "ethereum"],
    }


@pytest.mark.asyncio
async def test_root_serves_the_built_frontend():
    from web_app import app

    response = await request(app, "GET", "/")

    assert response.status_code == 200
    assert "ChainScope" in response.text
    assert 'id="root"' in response.text


@pytest.mark.asyncio
async def test_analyze_returns_structured_result_and_privacy_headers(monkeypatch):
    import web_app

    analyze = AsyncMock(return_value=SAMPLE_RESULT)
    monkeypatch.setattr(web_app, "analyze_wallet", analyze)

    response = await request(
        web_app.app,
        "POST",
        "/api/analyze",
        json={"address": SAMPLE_RESULT["address"]},
    )

    assert response.status_code == 200
    assert response.json() == SAMPLE_RESULT
    analyze.assert_awaited_once_with(SAMPLE_RESULT["address"], force_refresh=False)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_analyze_can_force_a_fresh_provider_snapshot(monkeypatch):
    import web_app

    analyze = AsyncMock(return_value=SAMPLE_RESULT)
    monkeypatch.setattr(web_app, "analyze_wallet", analyze)

    response = await request(
        web_app.app,
        "POST",
        "/api/analyze",
        json={"address": SAMPLE_RESULT["address"], "force_refresh": True},
    )

    assert response.status_code == 200
    analyze.assert_awaited_once_with(SAMPLE_RESULT["address"], force_refresh=True)
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "connect-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_invalid_address_returns_actionable_422(monkeypatch):
    import web_app

    monkeypatch.setattr(
        web_app,
        "analyze_wallet",
        AsyncMock(side_effect=ValueError("Enter a valid Solana or Ethereum wallet address.")),
    )

    response = await request(
        web_app.app,
        "POST",
        "/api/analyze",
        json={"address": "not-a-wallet"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Enter a valid Solana or Ethereum wallet address."
    }


@pytest.mark.asyncio
async def test_provider_errors_are_sanitized(monkeypatch):
    import web_app

    monkeypatch.setattr(
        web_app,
        "analyze_wallet",
        AsyncMock(side_effect=ServiceError("secret provider response")),
    )

    response = await request(
        web_app.app,
        "POST",
        "/api/analyze",
        json={"address": SAMPLE_RESULT["address"]},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Wallet data providers are temporarily unavailable. Try again shortly."
    }
    assert "secret provider response" not in response.text
