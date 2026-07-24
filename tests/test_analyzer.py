from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_analyze_ethereum_returns_structured_read_only_portfolio(monkeypatch):
    import analyzer

    address = "0x" + "1" * 40
    monkeypatch.setattr(analyzer, "get_eth_balance", AsyncMock(return_value=1.25))
    monkeypatch.setattr(analyzer, "get_eth_price", AsyncMock(return_value=2_000.0))

    result = await analyzer.analyze_wallet(address)

    assert result["address"] == address
    assert result["chain"] == "ethereum"
    assert result["native_asset"] == {
        "symbol": "ETH",
        "balance": 1.25,
        "price_usd": 2_000.0,
        "value_usd": 2_500.0,
    }
    assert result["total_value_usd"] == 2_500.0
    assert result["tokens"] == []
    assert result["token_summary"] == {
        "holding_count": 0,
        "valued_count": 0,
        "unavailable_count": 0,
        "value_usd": 0.0,
        "allocation_percent": 0.0,
    }
    assert result["warnings"] == []
    assert result["explorer_url"] == f"https://etherscan.io/address/{address}"
    assert result["updated_at"].endswith("Z")


@pytest.mark.asyncio
async def test_analyze_solana_aggregates_holdings_and_reports_partial_metadata(monkeypatch):
    import analyzer

    address = "11111111111111111111111111111111"
    accounts = [
        {
            "account": {
                "data": {
                    "parsed": {
                        "info": {
                            "mint": "mint-a",
                            "tokenAmount": {"uiAmount": 1.25},
                        }
                    }
                }
            }
        },
        {
            "account": {
                "data": {
                    "parsed": {
                        "info": {
                            "mint": "mint-a",
                            "tokenAmount": {"uiAmountString": "1.75"},
                        }
                    }
                }
            }
        },
        {
            "account": {
                "data": {
                    "parsed": {
                        "info": {
                            "mint": "mint-b",
                            "tokenAmount": {"uiAmount": 4},
                        }
                    }
                }
            }
        },
    ]
    monkeypatch.setattr(analyzer, "get_sol_balance", AsyncMock(return_value=2.0))
    monkeypatch.setattr(analyzer, "get_sol_price", AsyncMock(return_value=100.0))
    monkeypatch.setattr(analyzer, "get_token_accounts", AsyncMock(return_value=accounts))

    async def token_data(_session, mint, _sol_price, force_refresh=False):
        if mint == "mint-b":
            return None
        return {
            "name": "Alpha Token",
            "symbol": "ALPHA",
            "price_usd": 2.0,
            "price_in_sol": 0.02,
            "market_cap": 1_000_000,
            "volume_24h": 50_000,
            "liquidity": 100_000,
            "price_change_24h": 4.2,
            "url": "https://dexscreener.com/solana/alpha",
        }

    monkeypatch.setattr(analyzer, "get_token_data_dexscreener", token_data)

    result = await analyzer.analyze_wallet(address)

    assert result["chain"] == "solana"
    assert result["native_asset"]["value_usd"] == 200.0
    assert result["total_value_usd"] == 206.0
    assert result["token_summary"]["holding_count"] == 2
    assert result["token_summary"]["valued_count"] == 1
    assert result["token_summary"]["unavailable_count"] == 1
    assert result["token_summary"]["value_usd"] == 6.0
    assert result["token_summary"]["allocation_percent"] == pytest.approx(2.912621, rel=1e-6)
    assert result["warnings"] == ["Market data was unavailable for 1 holding."]
    assert result["tokens"] == [
        {
            "name": "Alpha Token",
            "symbol": "ALPHA",
            "mint": "mint-a",
            "balance": 3.0,
            "price_usd": 2.0,
            "value_usd": 6.0,
            "market_cap_usd": 1_000_000,
            "volume_24h_usd": 50_000,
            "liquidity_usd": 100_000,
            "price_change_24h_percent": 4.2,
            "market_url": "https://dexscreener.com/solana/alpha",
        }
    ]
    assert result["explorer_url"] == f"https://solscan.io/account/{address}"
